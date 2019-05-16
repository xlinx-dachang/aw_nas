# -*- coding: utf-8 -*-
"""
RL-based controllers
"""

import numpy as np
from torch import nn

from aw_nas import utils
from aw_nas.common import Rollout
from aw_nas.controller.base import BaseController
from aw_nas.controller.rl_networks import BaseRLControllerNet
from aw_nas.controller.rl_agents import BaseRLAgent

class RLController(BaseController, nn.Module):
    NAME = "rl"

    def __init__(self, search_space, device,
                 mode="eval",
                 controller_network_type="anchor_lstm", controller_network_cfg=None,
                 rl_agent_type="pg", rl_agent_cfg=None,
                 independent_cell_group=False):
        """
        Args:
            search_space (aw_nas.SearchSpace): The controller will sample arch seeds
                for this search space.
            device (str): `cuda` or `cpu`
            mode (str): `train` or `eval`
            independent_cell_group (bool): If true, use independent controller and agent
                for each cell group.

        .. warning::
            If `independent_cell_group` is set to true, do not merge anything across
            the graph of different cell groups.
        """
        super(RLController, self).__init__(search_space, mode)
        nn.Module.__init__(self)

        self.device = device
        self.independent_cell_group = independent_cell_group

        # handle cell groups here
        self.controllers = []
        self.agents = []
        rl_agent_cfg = rl_agent_cfg or {}
        controller_network_cfg = controller_network_cfg or {}
        cn_cls = BaseRLControllerNet.get_class_(controller_network_type)
        num_cnet = self.search_space.num_cell_groups if self.independent_cell_group else 1
        self.controllers = [cn_cls(self.search_space, self.device,
                                   **controller_network_cfg) for _ in range(num_cnet)]
        self.agents = [BaseRLAgent.get_class_(rl_agent_type)(cnet, **rl_agent_cfg)\
                       for cnet in self.controllers]

        self.controller = nn.ModuleList(self.controllers)

    def set_mode(self, mode):
        if mode == "train":
            nn.Module.train(self)
        elif mode == "eval":
            nn.Module.eval(self)
        else:
            raise Exception("Unrecognized mode: {}".format(mode))

    def forward(self, n=1): #pylint: disable=arguments-differ
        return self.sample(n=n)

    def sample(self, n=1):
        arch_lst = []
        log_probs_lst = []
        entropies_lst = []
        hidden = None
        for i_cg in range(self.search_space.num_cell_groups):
            # sample the arch for cell groups sequentially
            cn_idx = i_cg if self.independent_cell_group else 0
            arch, lprob, ent, hidden = self.controllers[cn_idx].sample(batch_size=n,
                                                                       prev_hidden=hidden)
            hidden = None if self.independent_cell_group else hidden
            arch_lst.append(arch)
            log_probs_lst.append(lprob)
            entropies_lst.append(ent)

        # merge the archs for different cell groups
        arch_lst = zip(*arch_lst)
        log_probs_lst = zip(*log_probs_lst)
        entropies_lst = zip(*entropies_lst)

        rollouts = [Rollout(arch, info={"log_probs": log_probs,
                                        "entropies": entropies},
                            search_space=self.search_space)
                    for arch, log_probs, entropies in zip(arch_lst, log_probs_lst,
                                                          entropies_lst)]
        return rollouts

    def save(self, path):
        for i, (controller, agent) in enumerate(zip(self.controllers, self.agents)):
            agent.save("{}_agent_{}".format(path, i))
            controller.save("{}_net_{}".format(path, i))

    def load(self, path):
        for i, (controller, agent) in enumerate(zip(self.controllers, self.agents)):
            agent.load("{}_agent_{}".format(path, i))
            controller.load("{}_net_{}".format(path, i))

    def step(self, rollouts, optimizer):
        if not self.independent_cell_group:
            # Single controller net and agent for all cell groups
            loss = self.agents[0].step(rollouts, optimizer)
        else:
            # One controller net and agent per cel group
            rollouts_lst = zip(*[self._split_rollout(r) for r in rollouts])
            loss = 0.
            for agent, splited_rollouts in zip(self.agents, rollouts_lst):
                loss += agent.step(splited_rollouts, optimizer)
            loss /= len(self.agents)
        return loss

    def summary(self, rollouts, prefix="", step=None):
        # log the total negative log prob and the entropies, averaged across the rollouts
        # also the averaged info for each cell group
        cg_logprobs = np.mean(np.array([[cg_lp.detach().cpu().numpy().sum() \
                                         for cg_lp in r.info["log_probs"]]\
                                        for r in rollouts]), 0)
        total_logprob = cg_logprobs.sum()
        cg_logprobs_str = ",".join(["{:.2f}".format(n) for n in cg_logprobs])
        cg_entros = np.mean(np.array([[cg_e.detach().cpu().numpy().sum() \
                                         for cg_e in r.info["entropies"]]\
                                        for r in rollouts]), 0)
        total_entro = cg_entros.sum()
        cg_entro_str = ",".join(["{:.2f}".format(n) for n in cg_entros])
        num = len(rollouts)

        self.logger.info("%s%d rollouts: -LOG_PROB: %.2f (%s) ; ENTROPY: %2f (%s)",
                         prefix, num,
                         -total_logprob, cg_logprobs_str,
                         total_entro, cg_entro_str)
        if step is not None and not self.writer.is_none():
            self.writer.add_scalar("log_prob", total_logprob, step)
            self.writer.add_scalar("entropy", total_entro, step)

    def on_epoch_start(self, epoch):
        super(RLController, self).on_epoch_start(epoch)
        [c.on_epoch_start(epoch) for c in self.controllers]
        [a.on_epoch_start(epoch) for a in self.agents]

    def on_epoch_end(self, epoch):
        super(RLController, self).on_epoch_end(epoch)
        [c.on_epoch_end(epoch) for c in self.controllers]
        [a.on_epoch_end(epoch) for a in self.agents]

    def setup_writer(self, writer):
        super(RLController, self).setup_writer(writer)
        [a.setup_writer(writer.get_sub_writer("rl_agent_{}".format(i))) \
         for i, a in enumerate(self.agents)]
        [c.setup_writer(writer.get_sub_writer("controller_network_{}".format(i))) \
         for i, c in enumerate(self.controllers)]

    @classmethod
    def get_default_config_str(cls):
        # Override. As there are sub-component in RLController
        all_str = super(RLController, cls).get_default_config_str()
        # Possible controller_network configs
        all_str += utils.component_sample_config_str("controller_network", prefix="#   ") + "\n"
        # Possible rl_agent configs
        all_str += utils.component_sample_config_str("rl_agent", prefix="#   ")
        return all_str

    @staticmethod
    def _split_rollout(rollout):
        rollouts = []
        for log_prob, ent in zip(rollout.info["log_probs"], rollout.info["entropies"]):
            rollouts.append(Rollout(rollout.arch, info={
                "log_probs": (log_prob,),
                "entropies": (ent,),
            }, search_space=rollout.search_space))
            rollouts[-1].perf = rollout.perf
        return rollouts
