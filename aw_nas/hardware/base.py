# -*- coding: utf-8 -*-

import abc
from collections import OrderedDict

import yaml
from six import StringIO

from aw_nas import utils
from aw_nas.base import Component
from aw_nas.common import SearchSpace


class BaseHardwareCompiler(Component):
    REGISTRY = "hardware_compiler"

    def __init__(self):
        super(BaseHardwareCompiler, self).__init__(schedule_cfg=None)

    @abc.abstractmethod
    def compile(self, compile_name, net_cfg, result_dir):
        pass

    @abc.abstractmethod
    def parse_file(
        self,
        prof_result_file,
        prof_prim_file,
        prim_to_ops,
        perf_fn=None,
        perf_names=("latency",),
    ):
        pass


class BaseHardwareObjectiveModel(Component):
    REGISTRY = "hardware_obj_model"

    def __init__(self, mixin_search_space, preprocessors, perf_name, schedule_cfg):
        super(BaseHardwareObjectiveModel, self).__init__(schedule_cfg)
        self.mixin_search_space = mixin_search_space
        self.preprocessor = Preprocessor(preprocessors)
        self.perf_name = perf_name

    def train(self, prof_nets):
        """
        Args:
            prof_nets: a list of dict, [{"primitives": [], "overall_performances": {}}, ...]
            performance: a list of value of each net's performance
        """
        processed_args = self.preprocessor(
            prof_nets, is_training=True, performance=self.perf_name)
        return self._train(processed_args)

    @abc.abstractmethod
    def _train(self, args):
        pass

    @abc.abstractmethod
    def predict(self, rollout):
        pass

    @abc.abstractmethod
    def save(self, path):
        pass

    @abc.abstractmethod
    def load(self, path):
        pass


class MixinProfilingSearchSpace(SearchSpace):

    @abc.abstractmethod
    def generate_profiling_primitives(self):
        pass

    @abc.abstractmethod
    def parse_profiling_primitives(
        self, prof_prims, prof_prims_cfg, hwobjmodel_type, hwobjmodel_cfg
    ):
        model = BaseHardwareObjectiveModel.get_class_(hwobjmodel_type)(
            prof_prims, prof_prims_cfg, **hwobjmodel_cfg
        )
        return model

    @classmethod
    def get_default_prof_config_str(cls):
        default_cfg = utils.get_default_argspec(cls.generate_profiling_primitives)
        stream = StringIO()
        cfg = OrderedDict(default_cfg)
        yaml.safe_dump(cfg, stream=stream, default_flow_style=False)
        return stream.getvalue()


class Preprocessor(Component):
    REGISTRY = "preprocessor_for_profiling"

    def __init__(self, preprocessors, schedule_cfg=None):
        super(Preprocessor, self).__init__(schedule_cfg)
        self.preprocessors = preprocessors

    def __call__(self, unpreprocessed, **kwargs):
        preps = [Preprocessor.get_class_(prep)() for prep in self.preprocessors]
        for prep in preps:
            unpreprocessed = prep(unpreprocessed, **kwargs)
        return unpreprocessed
