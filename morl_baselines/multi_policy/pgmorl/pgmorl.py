import random
import time
from copy import deepcopy
from typing import List, Optional, Tuple, Union

import gym
import mo_gym
import numpy as np
import torch as th
from scipy.optimize import least_squares

from morl_baselines.common.morl_algorithm import MOAgent
from morl_baselines.common.pareto import ParetoArchive
from morl_baselines.common.performance_indicators import hypervolume, sparsity
from morl_baselines.single_policy.ser.mo_ppo import MOPPO, MOPPONet, make_env


# Some code in this file has been adapted from the original code provided by the authors of the paper
# https://github.com/mit-gfx/PGMORL
class PerformancePredictor:
    def __init__(
        self,
        neighborhood_threshold: float = 0.1,
        sigma: float = 0.03,
        A_bound_min: float = 1.0,
        A_bound_max: float = 500.0,
        f_scale: float = 20.0,
    ):
        """
        Stores the performance deltas along with the used weights after each generation.
        Then, uses these stored samples to perform a regression for predicting the performance of using a given weight
        to train a given policy.
        """
        # Memory
        self.previous_performance = []
        self.next_performance = []
        self.used_weight = []

        # Prediction model parameters
        self.neighborhood_threshold = neighborhood_threshold
        self.A_bound_min = A_bound_min
        self.A_bound_max = A_bound_max
        self.f_scale = f_scale
        self.sigma = sigma

    def add(self, weight: np.ndarray, eval_before_pg: np.ndarray, eval_after_pg: np.ndarray):
        self.previous_performance.append(eval_before_pg)
        self.next_performance.append(eval_after_pg)
        self.used_weight.append(weight)

    def __build_model_and_predict(
        self,
        training_weights,
        training_deltas,
        training_next_perfs,
        current_dim,
        current_eval: np.ndarray,
        weight_candidate: np.ndarray,
        sigma: float,
    ):
        """
        Uses the hyperbolic model on the training data: weights, deltas and next_perfs to predict the next delta
        given the current evaluation and weight.
        :return: The expected delta from current_eval by using weight_candidate.
        """

        def __f(x, A, a, b, c):
            return A * (np.exp(a * (x - b)) - 1) / (np.exp(a * (x - b)) + 1) + c

        def __hyperbolic_model(params, x, y):
            # f = A * (exp(a(x - b)) - 1) / (exp(a(x - b)) + 1) + c
            return (
                params[0] * (np.exp(params[1] * (x - params[2])) - 1.0) / (np.exp(params[1] * (x - params[2])) + 1)
                + params[3]
                - y
            ) * w

        def __jacobian(params, x, y):
            A, a, b, _ = params[0], params[1], params[2], params[3]
            J = np.zeros([len(params), len(x)])
            # df_dA = (exp(a(x - b)) - 1) / (exp(a(x - b)) + 1)
            J[0] = ((np.exp(a * (x - b)) - 1) / (np.exp(a * (x - b)) + 1)) * w
            # df_da = A(x - b)(2exp(a(x-b)))/(exp(a(x-b)) + 1)^2
            J[1] = (A * (x - b) * (2.0 * np.exp(a * (x - b))) / ((np.exp(a * (x - b)) + 1) ** 2)) * w
            # df_db = A(-a)(2exp(a(x-b)))/(exp(a(x-b)) + 1)^2
            J[2] = (A * (-a) * (2.0 * np.exp(a * (x - b))) / ((np.exp(a * (x - b)) + 1) ** 2)) * w
            # df_dc = 1
            J[3] = w

            return np.transpose(J)

        train_x = []
        train_y = []
        w = []
        for i in range(len(training_weights)):
            train_x.append(training_weights[i][current_dim])
            train_y.append(training_deltas[i][current_dim])
            diff = np.abs(training_next_perfs[i] - current_eval)
            dist = np.linalg.norm(diff / np.abs(current_eval))
            coef = np.exp(-((dist / sigma) ** 2) / 2.0)
            w.append(coef)

        train_x = np.array(train_x)
        train_y = np.array(train_y)
        w = np.array(w)

        A_upperbound = np.clip(np.max(train_y) - np.min(train_y), 1.0, 500.0)
        initial_guess = np.ones(4)
        res_robust = least_squares(
            __hyperbolic_model,
            initial_guess,
            loss="soft_l1",
            f_scale=self.f_scale,
            args=(train_x, train_y),
            jac=__jacobian,
            bounds=([0, 0.1, -5.0, -500.0], [A_upperbound, 20.0, 5.0, 500.0]),
        )

        return __f(weight_candidate[current_dim], *res_robust.x)

    def predict_next_evaluation(self, weight_candidate: np.ndarray, policy_eval: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Use a part of the collected data (determined by the neighborhood threshold) to predict the performance
        after using weight to train the policy whose current evaluation is policy_eval.
        :param weight_candidate: weight candidate
        :param policy_eval: current evaluation of the policy
        :return: the delta prediction, along with the predicted next evaluations
        """
        neighbor_weights = []
        neighbor_deltas = []
        neighbor_next_perf = []
        current_sigma = self.sigma / 2.0
        current_neighb_threshold = self.neighborhood_threshold / 2.0
        # Iterates until we find at least 4 neighbors, enlarges the neighborhood at each iteration
        while len(neighbor_weights) < 4:
            # Enlarging neighborhood
            current_sigma *= 2.0
            current_neighb_threshold *= 2.0

            # Filtering for neighbors
            for previous_perf, next_perf, neighb_w in zip(self.previous_performance, self.next_performance, self.used_weight):
                if np.all(np.abs(previous_perf - policy_eval) < current_neighb_threshold * np.abs(policy_eval)) and tuple(
                    next_perf
                ) not in list(map(tuple, neighbor_next_perf)):
                    neighbor_weights.append(neighb_w)
                    neighbor_deltas.append(next_perf - previous_perf)
                    neighbor_next_perf.append(next_perf)

        # constructing a prediction model for each objective dimension, and using it to construct the delta predictions
        delta_predictions = [
            self.__build_model_and_predict(
                training_weights=neighbor_weights,
                training_deltas=neighbor_deltas,
                training_next_perfs=neighbor_next_perf,
                current_dim=obj_num,
                current_eval=policy_eval,
                weight_candidate=weight_candidate,
                sigma=current_sigma,
            )
            for obj_num in range(weight_candidate.size)
        ]
        delta_predictions = np.array(delta_predictions)
        return delta_predictions, delta_predictions + policy_eval


def generate_weights(delta_weight: float) -> np.ndarray:
    """
    Generates weights uniformly distributed over the objective dimensions. These weight vectors are separated by
    delta_weight distance.
    :param delta_weight: distance between weight vectors
    :return: all the candidate weights
    """
    return np.linspace((0.0, 1.0), (1.0, 0.0), int(1 / delta_weight) + 1, dtype=np.float32)


class PerformanceBuffer:
    """
    Divides the objective space in to n bins of size max_size.
    Stores the population
    """

    def __init__(self, num_bins: int, max_size: int, ref_point: np.ndarray):
        self.num_bins = num_bins
        self.max_size = max_size
        self.origin = -ref_point
        self.dtheta = np.pi / 2.0 / self.num_bins
        self.bins = [[] for _ in range(self.num_bins)]
        self.bins_evals = [[] for _ in range(self.num_bins)]

    @property
    def evaluations(self) -> List[np.ndarray]:
        # flatten
        return [e for l in self.bins_evals for e in l]

    @property
    def individuals(self) -> list:
        return [i for l in self.bins for i in l]

    def add(self, candidate, evaluation: np.ndarray):
        def center_eval(eval):
            # Objectives must be positive
            return np.clip(eval + self.origin, 0.0, float("inf"))

        centered_eval = center_eval(evaluation)
        norm_eval = np.linalg.norm(centered_eval)
        theta = np.arccos(np.clip(centered_eval[1] / (norm_eval + 1e-3), -1.0, 1.0))
        buffer_id = int(theta // self.dtheta)

        if buffer_id < 0 or buffer_id >= self.num_bins:
            return

        if len(self.bins[buffer_id]) < self.max_size:
            self.bins[buffer_id].append(deepcopy(candidate))
            self.bins_evals[buffer_id].append(evaluation)
        else:
            for i in range(len(self.bins[buffer_id])):
                stored_eval_centered = center_eval(self.bins_evals[buffer_id][i])
                if np.linalg.norm(stored_eval_centered) < np.linalg.norm(centered_eval):
                    self.bins[buffer_id][i] = deepcopy(candidate)
                    self.bins_evals[buffer_id][i] = evaluation
                    break


class PGMORL(MOAgent):
    """
    J. Xu, Y. Tian, P. Ma, D. Rus, S. Sueda, and W. Matusik,
    “Prediction-Guided Multi-Objective Reinforcement Learning for Continuous Robot Control,”
    in Proceedings of the 37th International Conference on Machine Learning,
    Nov. 2020, pp. 10607–10616. Available: https://proceedings.mlr.press/v119/xu20h.html

    https://people.csail.mit.edu/jiex/papers/PGMORL/paper.pdf
    https://people.csail.mit.edu/jiex/papers/PGMORL/supp.pdf
    """

    def __init__(
        self,
        env_id: str = "mo-halfcheetah-v4",
        ref_point: np.ndarray = np.array([0.0, -5.0]),
        num_envs: int = 4,
        pop_size: int = 6,
        warmup_iterations: int = 80,
        steps_per_iteration: int = 2048,
        limit_env_steps: int = int(5e6),
        evolutionary_iterations: int = 20,
        num_weight_candidates: int = 7,
        num_performance_buffer: int = 100,
        performance_buffer_size: int = 2,
        min_weight: float = 0.0,
        max_weight: float = 1.0,
        delta_weight: float = 0.2,
        env=None,
        gamma: float = 0.995,
        project_name: str = "MORL-baselines",
        experiment_name: str = "PGMORL",
        seed: int = 0,
        torch_deterministic: bool = True,
        log: bool = True,
        net_arch: List = [64, 64],
        num_minibatches: int = 32,
        update_epochs: int = 10,
        learning_rate: float = 3e-4,
        anneal_lr: bool = False,
        clip_coef: float = 0.2,
        ent_coef: float = 0.0,
        vf_coef: float = 0.5,
        clip_vloss: bool = True,
        max_grad_norm: float = 0.5,
        norm_adv: bool = True,
        target_kl: Optional[float] = None,
        gae: bool = True,
        gae_lambda: float = 0.95,
        device: Union[th.device, str] = "auto",
    ):
        super().__init__(env, device=device)
        # Env dimensions
        self.tmp_env = mo_gym.make(env_id)
        self.extract_env_info(self.tmp_env)
        self.env_id = env_id
        self.num_envs = num_envs
        assert isinstance(self.action_space, gym.spaces.Box), "only continuous action space is supported"
        self.tmp_env.close()
        self.gamma = gamma
        self.ref_point = ref_point

        # EA parameters
        self.pop_size = pop_size
        self.warmup_iterations = warmup_iterations
        self.steps_per_iteration = steps_per_iteration
        self.evolutionary_iterations = evolutionary_iterations
        self.num_weight_candidates = num_weight_candidates
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.delta_weight = delta_weight
        self.limit_env_steps = limit_env_steps
        self.max_iterations = self.limit_env_steps // self.steps_per_iteration // self.num_envs
        self.iteration = 0
        self.num_performance_buffer = num_performance_buffer
        self.performance_buffer_size = performance_buffer_size
        self.archive = ParetoArchive()
        self.population = PerformanceBuffer(
            num_bins=self.num_performance_buffer,
            max_size=self.performance_buffer_size,
            ref_point=self.ref_point,
        )
        self.predictor = PerformancePredictor()

        # PPO Parameters
        self.net_arch = net_arch
        self.batch_size = int(self.num_envs * self.steps_per_iteration)
        self.minibatch_size = int(self.batch_size // num_minibatches)
        self.update_epochs = update_epochs
        self.learning_rate = learning_rate
        self.anneal_lr = anneal_lr
        self.clip_coef = clip_coef
        self.vf_coef = vf_coef
        self.ent_coef = ent_coef
        self.max_grad_norm = max_grad_norm
        self.norm_adv = norm_adv
        self.target_kl = target_kl
        self.clip_vloss = clip_vloss
        self.gae_lambda = gae_lambda
        self.gae = gae

        # seeding
        self.seed = seed
        random.seed(self.seed)
        np.random.seed(self.seed)
        th.manual_seed(self.seed)
        th.backends.cudnn.deterministic = torch_deterministic

        # env setup
        self.num_envs = num_envs
        if env is None:
            self.env = mo_gym.MOSyncVectorEnv(
                [make_env(env_id, self.seed + i, i, experiment_name, self.gamma) for i in range(self.num_envs)]
            )
        else:
            raise ValueError("Environments should be vectorized for PPO. You should provide an environment id instead.")

        # Logging
        self.log = log
        if self.log:
            self.setup_wandb(project_name, experiment_name)

        self.networks = [
            MOPPONet(
                self.observation_shape,
                self.action_space.shape,
                self.reward_dim,
                self.net_arch,
            ).to(self.device)
            for _ in range(self.pop_size)
        ]

        weights = generate_weights(self.delta_weight)
        print(f"Warmup phase - sampled weights: {weights}")
        self.pop_size = len(weights)

        self.agents = [
            MOPPO(
                i,
                self.networks[i],
                weights[i],
                self.env,
                self.writer,
                gamma=self.gamma,
                device=self.device,
                seed=self.seed,
            )
            for i in range(self.pop_size)
        ]

    def get_config(self) -> dict:
        return {
            "env_id": self.env_id,
            "ref_point": self.ref_point,
            "num_envs": self.num_envs,
            "pop_size": self.pop_size,
            "warmup_iterations": self.warmup_iterations,
            "evolutionary_iterations": self.evolutionary_iterations,
            "steps_per_iteration": self.steps_per_iteration,
            "limit_env_steps": self.limit_env_steps,
            "max_iterations": self.max_iterations,
            "num_weight_candidates": self.num_weight_candidates,
            "num_performance_buffer": self.num_performance_buffer,
            "performance_buffer_size": self.performance_buffer_size,
            "min_weight": self.min_weight,
            "max_weight": self.max_weight,
            "delta_weight": self.delta_weight,
            "gamma": self.gamma,
            "seed": self.seed,
            "net_arch": self.net_arch,
            "batch_size": self.batch_size,
            "minibatch_size": self.minibatch_size,
            "update_epochs": self.update_epochs,
            "learning_rate": self.learning_rate,
            "anneal_lr": self.anneal_lr,
            "clip_coef": self.clip_coef,
            "vf_coef": self.vf_coef,
            "ent_coef": self.ent_coef,
            "max_grad_norm": self.max_grad_norm,
            "norm_adv": self.norm_adv,
            "target_kl": self.target_kl,
            "clip_vloss": self.clip_vloss,
            "gae": self.gae,
            "gae_lambda": self.gae_lambda,
        }

    def __train_all_agents(self):
        for i, agent in enumerate(self.agents):
            agent.train(self.start_time, self.iteration, self.max_iterations)

    def __eval_all_agents(self, evaluations_before_train: List[np.ndarray], add_to_prediction: bool = True):
        """
        Evaluates all agents and store their current performances on the buffer and pareto archive
        """
        for i, agent in enumerate(self.agents):
            _, _, _, discounted_reward = agent.policy_eval(self.env.envs[0], weights=agent.weights, writer=self.writer)
            # Storing current results
            self.population.add(agent, discounted_reward)
            self.archive.add(agent, discounted_reward)
            if add_to_prediction:
                self.predictor.add(
                    agent.weights.detach().cpu().numpy(),
                    evaluations_before_train[i],
                    discounted_reward,
                )
            evaluations_before_train[i] = discounted_reward

        print("Current pareto archive:")
        print(self.archive.evaluations)
        hv = hypervolume(self.ref_point, self.archive.evaluations)
        sp = sparsity(self.archive.evaluations)
        self.writer.add_scalar("charts/hypervolume", hv, self.iteration)
        self.writer.add_scalar("charts/sparsity", sp, self.iteration)

    def __task_weight_selection(self):
        """
        Chooses agents and weights to train at the next iteration based on the current population and prediction model.
        """
        candidate_weights = generate_weights(self.delta_weight / 2.0)  # Generates more weights than agents
        np.random.shuffle(candidate_weights)  # Randomize

        current_front = deepcopy(self.archive.evaluations)
        population = self.population.individuals
        population_eval = self.population.evaluations
        selected_tasks = []
        # For each worker, select a (policy, weight) tuple
        for i in range(len(self.agents)):
            max_improv = float("-inf")
            best_candidate = None
            best_eval = None
            best_predicted_eval = None

            # In each selection, look at every possible candidate in the current population and every possible weight generated
            for candidate, last_candidate_eval in zip(population, population_eval):
                # Pruning the already selected (candidate, weight) pairs
                candidate_tuples = [
                    (last_candidate_eval, weight)
                    for weight in candidate_weights
                    if (tuple(last_candidate_eval), tuple(weight)) not in selected_tasks
                ]

                # Prediction of improvements of each pair
                delta_predictions, predicted_evals = map(
                    list,
                    zip(
                        *[
                            self.predictor.predict_next_evaluation(weight, candidate_eval)
                            for candidate_eval, weight in candidate_tuples
                        ]
                    ),
                )
                # optimization criterion is a hypervolume - sparsity
                mixture_metrics = [
                    hypervolume(self.ref_point, current_front + [predicted_eval]) - sparsity(current_front + [predicted_eval])
                    for predicted_eval in predicted_evals
                ]
                # Best among all the weights for the current candidate
                current_candidate_weight = np.argmax(np.array(mixture_metrics))
                current_candidate_improv = np.max(np.array(mixture_metrics))

                # Best among all candidates, weight tuple update
                if max_improv < current_candidate_improv:
                    max_improv = current_candidate_improv
                    best_candidate = (
                        candidate,
                        candidate_tuples[current_candidate_weight][1],
                    )
                    best_eval = last_candidate_eval
                    best_predicted_eval = predicted_evals[current_candidate_weight]

            selected_tasks.append((tuple(best_eval), tuple(best_candidate[1])))
            # Append current estimate to the estimated front (to compute the next predictions)
            current_front.append(best_predicted_eval)

            # Assigns best predicted (weight-agent) pair to the worker
            copied_agent = deepcopy(best_candidate[0])
            copied_agent.global_step = self.agents[i].global_step
            copied_agent.id = i
            copied_agent.change_weights(deepcopy(best_candidate[1]))
            self.agents[i] = copied_agent

            print(f"Agent #{self.agents[i].id} - weights {best_candidate[1]}")
            print(
                f"current eval: {best_eval} - estimated next: {best_predicted_eval} - deltas {(best_predicted_eval - best_eval)}"
            )

    def train(self):
        # Init
        current_evaluations = [np.zeros(self.reward_dim) for _ in range(len(self.agents))]
        self.__eval_all_agents(current_evaluations, add_to_prediction=False)
        self.start_time = time.time()

        # Warmup
        for i in range(1, self.warmup_iterations + 1):
            self.writer.add_scalar("charts/warmup_iterations", i)
            print(f"Warmup iteration #{self.iteration}")
            self.__train_all_agents()
            self.iteration += 1
        self.__eval_all_agents(current_evaluations)

        # Evolution
        remaining_iterations = max(self.max_iterations - self.warmup_iterations, self.evolutionary_iterations)
        evolutionary_generation = 1
        while self.iteration < remaining_iterations:
            # Every evolutionary iterations, change the task - weight assignments
            self.__task_weight_selection()
            print(f"Evolutionary generation #{evolutionary_generation}")
            self.writer.add_scalar("charts/evolutionary_generation", evolutionary_generation)

            for _ in range(self.evolutionary_iterations):
                # Run training of every agent for evolutionary iterations.
                print(f"Evolutionary iteration #{self.iteration - self.warmup_iterations}")
                self.writer.add_scalar(
                    "charts/evolutionary_iterations",
                    self.iteration - self.warmup_iterations,
                )
                self.__train_all_agents()
                self.iteration += 1
            self.__eval_all_agents(current_evaluations)
            evolutionary_generation += 1

        print("Done training!")
        self.env.close()
        self.close_wandb()
