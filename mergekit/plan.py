# Copyright (C) 2024 Charles O. Goddard
#
# This software is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see http://www.gnu.org/licenses/.

import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional

import torch

from mergekit import merge_methods
from mergekit.architecture import (
    JSON_MAPPINGS,
    ArchitectureInfo,
    ConfiguredArchitectureInfo,
    WeightInfo,
    get_architecture_info,
)
from mergekit.common import ImmutableMap, ModelReference
from mergekit.config import (
    ConfigReader,
    InputSliceDefinition,
    MergeConfiguration,
    OutputSliceDefinition,
)
from mergekit.graph import Task
from mergekit.io.tasks import FinalizeModel, LoadTensor, SaveTensor, TensorWriterTask
from mergekit.matching import CollectDictTask, SpacePlanner
from mergekit.merge_methods import MergeMethod
from mergekit.merge_methods.tokenizer_permute import TokenizerPermutationMerge
from mergekit.options import MergeOptions
from mergekit.tokenizer import BuildTokenizer


class MergePlanner:
    config: MergeConfiguration
    clone_tensors: bool
    trust_remote_code: bool
    out_model_config: Any
    _writer_task: TensorWriterTask
    _method: MergeMethod
    _tasks: List[Task] = []
    _current_layers: int = 0
    _tokenizer_task: Optional[BuildTokenizer] = None
    _space_planner: Optional[SpacePlanner] = None

    def __init__(
        self,
        config: MergeConfiguration,
        out_path: str,
        options: MergeOptions,
        out_model_config: Any,
    ):
        self.config = config
        self.clone_tensors = options.clone_tensors
        self.trust_remote_code = options.trust_remote_code
        self.out_model_config = out_model_config
        self._method = merge_methods.get(config.merge_method)
        self._writer_task = TensorWriterTask(
            out_path=out_path,
            max_shard_size=options.out_shard_size,
            safe_serialization=options.safe_serialization,
        )

        if config.tokenizer_source:
            self._tokenizer_task = BuildTokenizer(
                base_model=config.base_model,
                referenced_models=tuple(config.referenced_models()),
                tokenizer_source=config.tokenizer_source,
                trust_remote_code=options.trust_remote_code,
            )

        if config.base_model and config.align_weights:
            self._space_planner = SpacePlanner(config.base_model)

        self.arch_dict: Dict[ModelReference, ConfiguredArchitectureInfo] = {}
        _models = config.referenced_models()
        base_model = _models[0]
        base_config = base_model.config(trust_remote_code=options.trust_remote_code)
        self.arch_info = get_architecture_info(base_config)

        for m in config.referenced_models():
            m_config = m.config(trust_remote_code=options.trust_remote_code)
            configured_arch_info = ConfiguredArchitectureInfo(
                info=get_architecture_info(m_config),
                config=m.config(m_config),
            )

            if config.align_weights:
                mapping = None
                for arch in base_config.architectures:
                    for destination_arch in m_config.architectures:
                        mapping = JSON_MAPPINGS.get(arch, {}).get(destination_arch)
                        if mapping:
                            break
                    if mapping:
                        break

                if mapping:
                    configured_arch_info = configured_arch_info.update_overrides(
                        mapping
                    )

            self.arch_dict[m] = configured_arch_info

    def normalize_config(self):
        # if models to merge are specified instead of output slices, compute them
        if self.config.models:
            if self.config.slices:
                raise RuntimeError(
                    "Must specify either models to merge or output slices"
                )

            slices_in = []
            base_model = None

            for model_in in self.config.models:
                model_info = self.arch_dict[model_in.model]
                slice = InputSliceDefinition(
                    layer_range=[0, model_info.num_layers()],
                    model=model_in.model,
                    parameters=model_in.parameters,
                )

                if model_in.model == self.config.base_model:
                    base_model = slice
                else:
                    slices_in.append(slice)

            if self.config.base_model:
                if not base_model:
                    base_model_info = self.arch_dict[self.config.base_model]
                    base_model = InputSliceDefinition(
                        layer_range=[0, base_model_info.num_layers()],
                        model=self.config.base_model,
                    )

                # Ensures base model is first in list
                slices_in = [base_model] + slices_in

            self.config.slices = [OutputSliceDefinition(sources=slices_in)]
            self.config.models = None

    def plan_tensor(
        self,
        weight: WeightInfo,
        weights_in: List[WeightInfo],
        models: List[ModelReference],
        cfg_reader: ConfigReader,
    ):
        tensor_merge_method = self._method
        if self._tokenizer_task and (weight.is_embed or weight.is_lm_head):
            tensor_merge_method = TokenizerPermutationMerge(
                tokenizer_task=self._tokenizer_task
            )

        cfg_g = cfg_reader.for_tensor(weight.name)
        global_params = {}
        for p in tensor_merge_method.parameters():
            global_params[p.name] = cfg_g.parameter(
                p.name, model=None, required=p.required, default=p.default_value
            )

        tensor_params = {}
        for model, weight_in in zip(models, weights_in):
            is_base = model == cfg_reader.config.base_model
            tensor_params[model] = {}
            cfg_m = cfg_reader.for_tensor(weight_in.name)
            for p in tensor_merge_method.tensor_parameters():
                tensor_params[model][p.name] = cfg_m.parameter(
                    p.name,
                    model=model,
                    required=p.required and not is_base,
                    default=p.default_value,
                )

        if self._space_planner:
            self._space_planner.add_weight(weight, zip(models, weights_in))

        input_weight_tasks = {}
        for model, weight_in in zip(models, weights_in):
            input_weight_tasks[model] = LoadTensor(
                model=model,
                tensor=weight_in.name,
                dtype=self.config.dtype,
                aliases=weight_in.aliases,
            )

        if self._space_planner:
            input_weight_tasks = {
                model: self._space_planner.align_tensor(model, weight, task)
                for model, task in input_weight_tasks.items()
            }

        gather_tensors = CollectDictTask[ModelReference, Optional[torch.Tensor]](
            tasks=ImmutableMap(data=input_weight_tasks)
        )

        tensor_task = tensor_merge_method.make_task(
            output_weight=weight,
            tensors=gather_tensors,
            parameters=ImmutableMap(data=global_params),
            tensor_parameters=ImmutableMap(
                data={
                    key: ImmutableMap(data=tensor_params[key]) for key in tensor_params
                }
            ),
            base_model=self.config.base_model,
        )
        save_task = SaveTensor(
            tensor_name=weight.name,
            tensor_task=tensor_task,
            writer_task=self._writer_task,
            clone=self.clone_tensors,
        )
        self._tasks.append(save_task)

    def plan_layer(
        self,
        sources: List[InputSliceDefinition],
        layer_offset: int,
        t: float,
        cfg_reader: ConfigReader,
    ):
        weights_out: List[WeightInfo] = self.arch_info.layer_weights(
            index=self._current_layers,
            config=self.out_model_config,
        )
        weights_in: List[List[WeightInfo]] = [
            self.arch_dict[s.model].layer_weights(index=s.layer_range[0] + layer_offset)
            for s in sources
        ]

        for idx, w_o in enumerate(weights_out):
            self.plan_tensor(
                weight=w_o,
                weights_in=[weights_in[j][idx] for j in range(len(weights_in))],
                models=[s.model for s in sources],
                cfg_reader=cfg_reader.with_t(t),
            )

        self._current_layers += 1

    def plan_slice(self, definition: OutputSliceDefinition):
        slice_lengths = [
            s.layer_range[1] - s.layer_range[0] for s in definition.sources
        ]
        if not all(s == slice_lengths[0] for s in slice_lengths):
            raise RuntimeError(
                "All inputs to a slice must contain the same number of layers"
            )
        num_layers = slice_lengths[0]

        cfg_reader = ConfigReader(config=self.config, slice_out=definition, t=0)
        for idx in range(num_layers):
            # compute t for interpolated gradients
            if num_layers > 1:
                t = idx / (num_layers - 1)
            else:
                t = 1

            self.plan_layer(
                definition.sources,
                layer_offset=idx,
                t=t,
                cfg_reader=cfg_reader,
            )

    def plan(self):
        self.normalize_config()
        self._tasks = []

        if self._space_planner:
            for space in self.arch_info.procedural_spaces(config=self.out_model_config):
                self._space_planner.add_procedural_space(space)

        _models = [s.model for s in self.config.slices[0].sources]

        for weight_infos in zip(
            *[
                self.arch_dict[m].info.pre_weights(config=self.out_model_config)
                for m in _models
            ]
        ):
            self.plan_tensor(
                weight_infos[0],
                list(weight_infos),
                _models,
                ConfigReader(
                    config=self.config,
                    t=0,
                    tensor_name=weight_infos[0].name,
                ).for_out_slice(self.config.slices[0]),
            )

        for out_slice in self.config.slices:
            self.plan_slice(out_slice)

        _models = [s.model for s in self.config.slices[-1].sources]
        for weight_infos in zip(
            *[
                self.arch_dict[m].info.post_weights(config=self.out_model_config)
                for m in _models
            ],
        ):
            self.plan_tensor(
                weight_infos[0],
                list(weight_infos),
                _models,
                ConfigReader(
                    config=self.config,
                    t=1,
                    tensor_name=weight_infos[0].name,
                ).for_out_slice(self.config.slices[-1]),
            )

        self._tasks.append(
            FinalizeModel(
                tensor_save_tasks=tuple(self._tasks), writer_task=self._writer_task
            )
        )
        res = list(self._tasks)
        if self._tokenizer_task:
            res.append(self._tokenizer_task)
        return res
