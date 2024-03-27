import numpy as np
import torch
import torch.optim as optim
from torch import nn as nn

from collections import OrderedDict

import lifelong_rl.torch.pytorch_util as ptu
from lifelong_rl.torch.distributions import TanhNormal
from lifelong_rl.util.eval_util import create_stats_ordered_dict
from lifelong_rl.core.rl_algorithms.torch_rl_algorithm import TorchTrainer


class SACTrainer(TorchTrainer):

    """
    Soft Actor Critic (Haarnoja et al. 2018).
    Continuous maximum Q-learning algorithm with parameterized actor.
    """

    def __init__(
            self,
            env,                                # Associated environment for learning
            policy,                             # Associated policy (should be TanhGaussian)
            qf1,                                # Q function #1
            qf2,                                # Q function #2
            target_qf1,                         # Slow updater to Q function #1
            target_qf2,                         # Slow updater to Q function #2

            discount=0.99,                      # Discount factor
            reward_scale=1.0,                   # Scaling of rewards to modulate entropy bonus
            use_automatic_entropy_tuning=True,  # Whether to use the entropy-constrained variant
            target_entropy=None,                # Target entropy for entropy-constraint variant

            policy_lr=3e-4,                     # Learning rate of policy and entropy weight
            qf_lr=3e-4,                         # Learning rate of Q functions
            optimizer_class=optim.Adam,         # Class of optimizer for all networks

            soft_target_tau=5e-3,               # Rate of update of target networks
            target_update_period=1,             # How often to update target networks
    ):
        super().__init__()

        self.env = env
        self.policy = policy
        self.qf1 = qf1
        self.qf2 = qf2
        self.target_qf1 = target_qf1
        self.target_qf2 = target_qf2

        self.discount = discount
        self.reward_scale = reward_scale
        self.soft_target_tau = soft_target_tau
        self.target_update_period = target_update_period

        self.use_automatic_entropy_tuning = use_automatic_entropy_tuning
        if self.use_automatic_entropy_tuning:
            if target_entropy:
                self.target_entropy = target_entropy
            else:
                # Heuristic value: dimension of action space
                self.target_entropy = -np.prod(self.env.action_space.shape).item()
            self.log_alpha = ptu.zeros(1, requires_grad=True)
            self.alpha_optimizer = optimizer_class(
                [self.log_alpha],
                lr=policy_lr,
            )

        self.qf_criterion = nn.MSELoss()
        self.vf_criterion = nn.MSELoss()

        self.policy_optimizer = optimizer_class(
            self.policy.parameters(),
            lr=policy_lr,
        )
        self.qf1_optimizer = optimizer_class(
            self.qf1.parameters(),
            lr=qf_lr,
        )
        self.qf2_optimizer = optimizer_class(
            self.qf2.parameters(),
            lr=qf_lr,
        )

        self.eval_statistics = OrderedDict()
        self._n_train_steps_total = 0
        self._need_to_update_eval_statistics = True

        def mask(self, mask_prob, batch_size):
            effective_batch_size = self.num_qs * mask_prob
            masks = np.zeros((2, batch_size))
            for i in range(batch_size):
                masks[:int(effective_batch_size), i] = 1
                random.shuffle(masks[:, i])
            masks = torch.tensor(masks)
            return masks.unsqueeze(2).to(device)

        def pre_train(self, batch):  # 预训练一个策略或者采样动作,先纯粹的拟合采样出来的q和策略如何。

            rewards = torch.tensor(batch['rewards']).to(device)  # shape=(256, 1)
            terminals = torch.tensor(batch['terminals'])  # shape=(256, 1)
            obs = torch.tensor(batch['observations']).to(device)  # shape=(256, 17)
            actions = torch.tensor(batch['actions']).to(device)  # shape=(256, 6)
            next_obs = torch.tensor(batch['next_observations'])  # shape=(256, 17)
            batch_size = rewards.size()[0]
            """ Policy and Alpha Loss

            """
            self._current_pre_epoch += 1
            # batch data 的 obs 通过当前 policy 得到 new_obs_actions
            new_curr_actions, _, _, new_curr_log_pi, *_ = self.policy(obs, reparameterize=True,
                                                                      return_log_prob=True)  # 现在的策略如何让它变到能和数据集中的下一步一样
            a = self.mask(0.6, batch_size)
            if self._current_epoch % 10 == 0:
                qf1_loss=self.qf_criterion(self.qf1(obs, actions)*a[1], rewards*a[1])
                self.qf1_optimizer.zero_grad()
                qf1_loss.backward()
                self.qf1_optimizer.step()
                qf2_loss = self.qf_criterion(self.qf2(obs, actions)*a[2], rewards*a[2])
                self.qf2_optimizer.zero_grad()
                qf2_loss.backward()
                self.qf2_optimizer.step()
            policy_loss = (actions - new_curr_actions).mean()
            self.policy1_optimizer.zero_grad()
            policy_loss.backward(retain_graph=True)
            self.policy1_optimizer.step()
            if self._current_epoch % 50 == 0:
                target_q_values = torch.min(
                    self.target_qf1(next_obs, new_next_actions),
                    self.target_qf2(next_obs, new_next_actions),
                ) - alpha * new_log_pi

                self.try_update_target_networks()

    def train_from_torch(self, batch,w):
        obs = batch['observations']
        next_obs = batch['next_observations']
        actions = batch['actions']
        rewards = batch['rewards']
        terminals = batch.get('terminals', ptu.zeros(rewards.shape[0], 1))

        """
        Policy and Alpha Loss
        """
        _, policy_mean, policy_logstd, *_ = self.policy(obs)
        dist = TanhNormal(policy_mean, policy_logstd.exp())
        new_obs_actions, log_pi = dist.rsample_and_logprob()
        log_pi = log_pi.sum(dim=-1, keepdims=True)
        if self.use_automatic_entropy_tuning:
            alpha_loss = -(self.log_alpha * (log_pi + self.target_entropy).detach()).mean()
            alpha = self.log_alpha.exp()
        else:
            alpha_loss = 0
            alpha = 1

        q_new_actions = torch.min(
            self.qf1(obs, new_obs_actions),
            self.qf2(obs, new_obs_actions),
        )
        policy_loss = (alpha * log_pi - q_new_actions).mean()

        """
        QF Loss
        """
        q1_pred = self.qf1(obs, actions)
        q2_pred = self.qf2(obs, actions)
        _, next_policy_mean, next_policy_logstd, *_ = self.policy(next_obs)
        next_dist = TanhNormal(next_policy_mean, next_policy_logstd.exp())
        new_next_actions, new_log_pi = next_dist.rsample_and_logprob()
        new_log_pi = new_log_pi.sum(dim=-1, keepdims=True)
        target_q_values = torch.min(
            self.target_qf1(next_obs, new_next_actions),
            self.target_qf2(next_obs, new_next_actions),
        ) - alpha * new_log_pi

        future_values = (1. - terminals) * self.discount * target_q_values
        q_target = self.reward_scale * rewards + future_values
        qf1_loss = self.qf_criterion(q1_pred, q_target.detach())
        qf2_loss = self.qf_criterion(q2_pred, q_target.detach())

        if self.use_automatic_entropy_tuning:
            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()

        self.policy_optimizer.zero_grad()
        policy_loss.backward()
        self.policy_optimizer.step()

        self.qf1_optimizer.zero_grad()
        qf1_loss.backward()
        self.qf1_optimizer.step()

        self.qf2_optimizer.zero_grad()
        qf2_loss.backward()
        self.qf2_optimizer.step()

        self._n_train_steps_total += 1

        self.try_update_target_networks()

        """
        Save some statistics for eval
        """
        if self._need_to_update_eval_statistics:
            self._need_to_update_eval_statistics = False

            policy_loss = (log_pi - q_new_actions).mean()
            policy_avg_std = torch.exp(policy_logstd).mean()

            self.eval_statistics['QF1 Loss'] = np.mean(ptu.get_numpy(qf1_loss))
            self.eval_statistics['QF2 Loss'] = np.mean(ptu.get_numpy(qf2_loss))
            self.eval_statistics['Policy Loss'] = np.mean(ptu.get_numpy(
                policy_loss
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Q1 Predictions',
                ptu.get_numpy(q1_pred),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Q2 Predictions',
                ptu.get_numpy(q2_pred),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Q Targets',
                ptu.get_numpy(q_target),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Log Pis',
                ptu.get_numpy(log_pi),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Policy mu',
                ptu.get_numpy(policy_mean),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Policy log std',
                ptu.get_numpy(policy_logstd),
            ))
            self.eval_statistics['Policy std'] = np.mean(ptu.get_numpy(policy_avg_std))

            if self.use_automatic_entropy_tuning:
                self.eval_statistics['Alpha'] = alpha.item()
                self.eval_statistics['Alpha Loss'] = alpha_loss.item()

        self._n_train_steps_total += 1
        return q_target

    def try_update_target_networks(self):
        if self._n_train_steps_total % self.target_update_period == 0:
            self.update_target_networks()

    def update_target_networks(self):
        ptu.soft_update_from_to(
            self.qf1, self.target_qf1, self.soft_target_tau
        )
        ptu.soft_update_from_to(
            self.qf2, self.target_qf2, self.soft_target_tau
        )

    def get_diagnostics(self):
        return self.eval_statistics

    def end_epoch(self, epoch):
        self._need_to_update_eval_statistics = True

    @property
    def networks(self):
        return [
            self.policy,
            self.qf1,
            self.qf2,
            self.target_qf1,
            self.target_qf2,
        ]

    def get_snapshot(self):
        return dict(
            policy=self.policy,
            qf1=self.qf1,
            qf2=self.qf2,
            target_qf1=self.qf1,
            target_qf2=self.qf2,
        )