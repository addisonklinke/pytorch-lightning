# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
from typing import List, Optional, Sequence, Union

import torch

from pytorch_lightning.accelerators.accelerator import Accelerator
from pytorch_lightning.accelerators.cpu import CPUAccelerator
from pytorch_lightning.accelerators.gpu import GPUAccelerator
from pytorch_lightning.accelerators.ipu import IPUAccelerator
from pytorch_lightning.accelerators.tpu import TPUAccelerator
from pytorch_lightning.plugins import (
    ApexMixedPrecisionPlugin,
    DataParallelPlugin,
    DDP2Plugin,
    DDPFullyShardedPlugin,
    DDPPlugin,
    DDPShardedPlugin,
    DDPSpawnPlugin,
    DDPSpawnShardedPlugin,
    DeepSpeedPlugin,
    DeepSpeedPrecisionPlugin,
    DoublePrecisionPlugin,
    FullyShardedNativeMixedPrecisionPlugin,
    HorovodPlugin,
    IPUPlugin,
    IPUPrecisionPlugin,
    NativeMixedPrecisionPlugin,
    PrecisionPlugin,
    ShardedNativeMixedPrecisionPlugin,
    SingleDevicePlugin,
    SingleTPUPlugin,
    TPUHalfPrecisionPlugin,
    TPUSpawnPlugin,
    TrainingTypePlugin,
    TrainingTypePluginsRegistry,
)
from pytorch_lightning.plugins.environments import (
    ClusterEnvironment,
    KubeflowEnvironment,
    LightningEnvironment,
    SLURMEnvironment,
    TorchElasticEnvironment,
)
from pytorch_lightning.tuner.auto_gpu_select import pick_multiple_gpus
from pytorch_lightning.utilities import (
    _APEX_AVAILABLE,
    _HOROVOD_AVAILABLE,
    _IPU_AVAILABLE,
    _NATIVE_AMP_AVAILABLE,
    _TPU_AVAILABLE,
    AMPType,
    device_parser,
    DeviceType,
    DistributedType,
    rank_zero_deprecation,
    rank_zero_info,
    rank_zero_warn,
)
from pytorch_lightning.utilities.exceptions import MisconfigurationException

if _HOROVOD_AVAILABLE:
    import horovod.torch as hvd

log = logging.getLogger(__name__)


class AcceleratorConnector(object):

    def __init__(
        self,
        num_processes,
        tpu_cores,
        ipus,
        distributed_backend,
        auto_select_gpus,
        gpus,
        num_nodes,
        sync_batchnorm,
        benchmark,
        replace_sampler_ddp,
        deterministic,
        precision,
        amp_type,
        amp_level,
        plugins,
    ):
        # initialization
        self._device_type = DeviceType.CPU
        self._distrib_type = None

        self.num_processes = num_processes
        self.tpu_cores = device_parser.parse_tpu_cores(tpu_cores)
        self.ipus = ipus
        self.distributed_backend = distributed_backend
        self.auto_select_gpus = auto_select_gpus
        self.gpus = gpus
        self.num_nodes = num_nodes
        self.sync_batchnorm = sync_batchnorm
        self.benchmark = benchmark
        self.replace_sampler_ddp = replace_sampler_ddp
        self.deterministic = deterministic
        self.precision = precision
        self.amp_type = amp_type.lower() if isinstance(amp_type, str) else None
        self.amp_level = amp_level
        self.is_slurm_managing_tasks = False

        self._precision_plugin: Optional[PrecisionPlugin] = None
        self._training_type_plugin: Optional[TrainingTypePlugin] = None
        self._cluster_environment: Optional[ClusterEnvironment] = None

        plugins = plugins if plugins is not None else []

        if isinstance(plugins, str):
            plugins = [plugins]

        if not isinstance(plugins, Sequence):
            plugins = [plugins]

        self.plugins = plugins

        # for gpus allow int, string and gpu list
        if auto_select_gpus and isinstance(gpus, int):
            self.gpus = pick_multiple_gpus(gpus)

        self.parallel_device_ids = device_parser.parse_gpu_ids(self.gpus)

        self.set_distributed_mode()
        self.configure_slurm_ddp()

        self.handle_given_plugins()

        self._training_type_plugin_resolved = False
        self.accelerator = self.select_accelerator()

        # override dist backend when using tpus
        if self.on_tpu:
            self.distributed_backend = "tpu"

        # init flags for SLURM+DDP to work
        self.world_size = 1
        self.interactive_ddp_procs = []
        self.global_rank = 0

        # benchmarking
        # TODO: should this be moved to GPU accelerator?
        torch.backends.cudnn.benchmark = self.benchmark

        # determinism for cudnn
        # TODO: should this be moved to GPU accelerator?
        torch.backends.cudnn.deterministic = deterministic
        if deterministic:
            # fixing non-deterministic part of horovod
            # https://github.com/PyTorchLightning/pytorch-lightning/pull/1572/files#r420279383
            os.environ["HOROVOD_FUSION_THRESHOLD"] = str(0)

        self.replace_sampler_ddp = replace_sampler_ddp

    def handle_given_plugins(self) -> None:

        training_type = None
        precision = None
        cluster_environment = None

        for plug in self.plugins:
            if isinstance(plug, str) and plug in TrainingTypePluginsRegistry:
                if training_type is None:
                    training_type = TrainingTypePluginsRegistry.get(plug)
                else:
                    raise MisconfigurationException(
                        'You can only specify one precision and one training type plugin.'
                        ' Found more than 1 training type plugin:'
                        f' {TrainingTypePluginsRegistry[plug]["plugin"]} registered to {plug}'
                    )
            if isinstance(plug, str):
                # Reset the distributed type as the user has overridden training type
                # via the plugins argument
                self._distrib_type = None
                self.set_distributed_mode(plug)

            elif isinstance(plug, TrainingTypePlugin):
                if training_type is None:
                    training_type = plug

                else:
                    raise MisconfigurationException(
                        'You can only specify one precision and one training type plugin.'
                        f' Found more than 1 training type plugin: {type(plug).__name__}'
                    )
            elif isinstance(plug, PrecisionPlugin):
                if precision is None:
                    precision = plug
                else:
                    raise MisconfigurationException(
                        'You can only specify one precision and one training type plugin.'
                        f' Found more than 1 precision plugin: {type(plug).__name__}'
                    )

            elif isinstance(plug, ClusterEnvironment):
                if cluster_environment is None:
                    cluster_environment = plug
                else:
                    raise MisconfigurationException(
                        'You can only specify one cluster environment. Found more than 1 cluster environment plugin'
                    )
            else:
                raise MisconfigurationException(
                    f'Found invalid type for plugin {plug}. Expected a precision or training type plugin.'
                )

        self._training_type_plugin = training_type
        self._precision_plugin = precision
        self._cluster_environment = cluster_environment or self.select_cluster_environment()

    @property
    def precision_plugin(self) -> PrecisionPlugin:
        if self._precision_plugin is None:
            self._precision_plugin = self.select_precision_plugin()
        return self._precision_plugin

    @property
    def training_type_plugin(self) -> TrainingTypePlugin:
        if self._training_type_plugin_resolved:
            # avoid calling `resolve_training_type_plugin` multiple times
            return self._training_type_plugin
        if self._training_type_plugin is None:
            self._training_type_plugin = self.select_training_type_plugin()
        self._training_type_plugin = self.resolve_training_type_plugin(self._training_type_plugin)
        self._training_type_plugin_resolved = True

        return self._training_type_plugin

    @property
    def cluster_environment(self) -> ClusterEnvironment:
        return self._cluster_environment

    @property
    def on_cpu(self) -> bool:
        return self._device_type == DeviceType.CPU

    @property
    def on_tpu(self) -> bool:
        return self.tpu_cores is not None

    @property
    def on_ipu(self) -> bool:
        return self.ipus is not None

    @property
    def tpu_id(self) -> Optional[int]:
        if self.on_tpu and isinstance(self.tpu_cores, list):
            return self.tpu_cores[0]

        return None

    @property
    def on_gpu(self) -> bool:
        gpus = self.parallel_device_ids
        return gpus is not None and len(gpus) > 0 and torch.cuda.is_available()

    @property
    def use_dp(self) -> bool:
        return self._distrib_type == DistributedType.DP

    @property
    def use_ddp(self) -> bool:
        return self._distrib_type in (
            DistributedType.DDP,
            DistributedType.DDP_SPAWN,
            DistributedType.DDP_SHARDED,
            DistributedType.DDP_SHARDED_SPAWN,
            DistributedType.DDP_FULLY_SHARDED,
            DistributedType.DEEPSPEED,
            DistributedType.TPU_SPAWN,
        )

    @property
    def use_ddp2(self) -> bool:
        return self._distrib_type == DistributedType.DDP2

    @property
    def use_horovod(self) -> bool:
        return self._distrib_type == DistributedType.HOROVOD

    @property
    def use_deepspeed(self) -> bool:
        return self._distrib_type == DistributedType.DEEPSPEED

    @property
    def _is_sharded_training_type(self) -> bool:
        return isinstance(self.training_type_plugin, (DDPShardedPlugin, DDPSpawnShardedPlugin))

    @property
    def _is_fully_sharded_training_type(self) -> bool:
        return isinstance(self.training_type_plugin, DDPFullyShardedPlugin)

    @property
    def is_distributed(self) -> bool:
        # Used for custom plugins.
        # Custom plugins should implement is_distributed property.
        if hasattr(self.training_type_plugin, 'is_distributed') and not self.on_tpu:
            return self.training_type_plugin.is_distributed
        is_distributed = self.use_ddp or self.use_ddp2 or self.use_horovod
        if self.on_tpu:
            is_distributed |= self.training_type_plugin.is_distributed
        return is_distributed

    @property
    def num_gpus(self) -> int:
        gpus = self.parallel_device_ids
        if gpus is None:
            return 0
        return len(gpus)

    @property
    def parallel_devices(self) -> List[Union[torch.device, int]]:
        if self.on_gpu:
            devices = [torch.device("cuda", i) for i in self.parallel_device_ids]
        elif self.on_tpu:
            # explicitly don't make a tpu device here!
            # https://github.com/PyTorchLightning/pytorch-lightning/issues/3169
            if isinstance(self.tpu_cores, int):
                devices = list(range(self.tpu_cores))
        elif self.on_ipu:
            if isinstance(self.ipus, int):
                devices = list(range(self.ipus))
        else:
            devices = [torch.device("cpu")] * self.num_processes
        return devices

    @property
    def root_gpu(self) -> Optional[int]:
        return self.accelerator.root_device.index if not isinstance(
            self.accelerator, (IPUAccelerator, TPUAccelerator)
        ) else None

    @property
    def is_training_type_in_plugins(self) -> bool:
        return any(isinstance(plug, str) and plug in TrainingTypePluginsRegistry for plug in self.plugins)

    @property
    def is_using_torchelastic(self) -> bool:
        """
        .. deprecated:: v1.3
            Will be removed in v1.5.0.
        Returns:
            ``True`` if the current process was launched using the torchelastic command.
        """
        rank_zero_deprecation(
            "The property `AcceleratorConnector.is_using_torchelastic` was deprecated in v1.3"
            " and will be removed in 1.5. Use `TorchElasticEnvironment.is_using_torchelastic()` instead.",
        )
        return TorchElasticEnvironment.is_using_torchelastic()

    def select_precision_plugin(self) -> PrecisionPlugin:
        # set precision type
        self.amp_type = AMPType.from_str(self.amp_type)

        if self.on_ipu:
            return IPUPrecisionPlugin(self.precision)

        if self._distrib_type == DistributedType.DEEPSPEED or isinstance(self._training_type_plugin, DeepSpeedPlugin):
            return DeepSpeedPrecisionPlugin(self.precision)

        if self.precision == 32:
            return PrecisionPlugin()
        elif self.precision == 64:
            return DoublePrecisionPlugin()
        elif self.precision == 16:
            if self.on_tpu:
                return TPUHalfPrecisionPlugin()

            if self.amp_type == AMPType.NATIVE:
                if self.on_cpu:
                    raise MisconfigurationException(
                        "You have asked for native AMP on CPU, but AMP is only available on GPU."
                    )
                elif not _NATIVE_AMP_AVAILABLE:
                    msg = "You have asked for native AMP but your PyTorch version does not support it." \
                          " Consider upgrading with `pip install torch>=1.6`."
                    if _APEX_AVAILABLE:
                        self.amp_type = AMPType.APEX
                        msg += " We will attempt to use NVIDIA Apex for this session."
                        rank_zero_warn(msg)
                    else:
                        raise MisconfigurationException(msg)
                else:
                    log.info("Using native 16bit precision.")
                    if self._is_sharded_training_type:
                        return ShardedNativeMixedPrecisionPlugin()
                    if self._is_fully_sharded_training_type:
                        return FullyShardedNativeMixedPrecisionPlugin()
                    return NativeMixedPrecisionPlugin()

            if self.amp_type == AMPType.APEX:
                if not _APEX_AVAILABLE:
                    raise MisconfigurationException(
                        "You have asked for Apex AMP but you have not installed it yet."
                        " Install apex first using this guide: https://github.com/NVIDIA/apex#linux"
                    )
                if self._is_sharded_training_type or self._is_fully_sharded_training_type:
                    raise MisconfigurationException(
                        "Sharded Plugin is not supported with Apex AMP,"
                        " please using native AMP for 16-bit precision."
                    )
                log.info("Using APEX 16bit precision.")
                return ApexMixedPrecisionPlugin(self.amp_level)

        raise NotImplementedError("We only support precisions 64, 32 and 16!")

    def select_training_type_plugin(self) -> TrainingTypePlugin:
        if isinstance(
            self.distributed_backend, Accelerator
        ) and self.distributed_backend.training_type_plugin is not None:
            plugin = self.distributed_backend.training_type_plugin
        elif self.use_ddp2:
            plugin = DDP2Plugin(
                parallel_devices=self.parallel_devices,
                cluster_environment=self.cluster_environment,
            )
        elif self.use_ddp and self.use_deepspeed:
            plugin = DeepSpeedPlugin(
                cluster_environment=self.select_cluster_environment(), parallel_devices=self.parallel_devices
            )
        elif self.use_ddp:
            use_slurm_ddp = self.use_ddp and self.is_slurm_managing_tasks
            use_torchelastic_ddp = self.use_ddp and TorchElasticEnvironment.is_using_torchelastic()
            use_kubeflow_ddp = self.use_ddp and KubeflowEnvironment.is_using_kubeflow()
            use_ddp_spawn = self._distrib_type == DistributedType.DDP_SPAWN
            use_ddp_cpu_spawn = self.use_ddp and self.on_cpu
            use_tpu_spawn = self.on_tpu and self._distrib_type == DistributedType.TPU_SPAWN
            use_ddp_cpu_torch_elastic = use_ddp_cpu_spawn and TorchElasticEnvironment.is_using_torchelastic()
            use_ddp_cpu_kubeflow = use_ddp_cpu_spawn and KubeflowEnvironment.is_using_kubeflow()
            use_ddp_cpu_slurm = use_ddp_cpu_spawn and self.is_slurm_managing_tasks
            use_ddp_sharded = self._distrib_type == DistributedType.DDP_SHARDED
            use_ddp_sharded_spawn = self._distrib_type == DistributedType.DDP_SHARDED_SPAWN
            use_ddp_fully_sharded = self._distrib_type == DistributedType.DDP_FULLY_SHARDED

            # TODO: decouple from TE
            # ddp script mode uses the same flags as TE
            if os.environ.get("PL_IN_DDP_SUBPROCESS", False):
                use_torchelastic_ddp = False

            if use_tpu_spawn:
                ddp_plugin_cls = TPUSpawnPlugin
            elif use_ddp_sharded:
                ddp_plugin_cls = DDPShardedPlugin
            elif use_ddp_sharded_spawn:
                ddp_plugin_cls = DDPSpawnShardedPlugin
            elif (
                use_ddp_cpu_slurm or use_slurm_ddp or use_ddp_cpu_torch_elastic or use_torchelastic_ddp
                or use_kubeflow_ddp or use_ddp_cpu_kubeflow
            ):
                ddp_plugin_cls = DDPPlugin
            elif use_ddp_spawn or use_ddp_cpu_spawn:
                ddp_plugin_cls = DDPSpawnPlugin
            elif use_ddp_fully_sharded:
                ddp_plugin_cls = DDPFullyShardedPlugin
            else:
                ddp_plugin_cls = DDPPlugin

            plugin = ddp_plugin_cls(
                parallel_devices=self.parallel_devices,
                cluster_environment=self.cluster_environment,
            )
        elif self.use_dp:
            plugin = DataParallelPlugin(parallel_devices=self.parallel_devices)
        elif self.use_horovod:
            plugin = HorovodPlugin(parallel_devices=self.parallel_devices)
        elif self.on_tpu and isinstance(self.tpu_cores, list):
            plugin = SingleTPUPlugin(self.tpu_id)
        elif self.on_ipu:
            plugin = IPUPlugin(parallel_devices=self.parallel_devices)
        else:
            single_gpu_ordinal = device_parser.determine_root_gpu_device(self.parallel_device_ids)
            plugin = SingleDevicePlugin(device=torch.device(f"cuda:{single_gpu_ordinal}" if self.on_gpu else "cpu"))
        return plugin

    def resolve_training_type_plugin(self, training_type: TrainingTypePlugin) -> TrainingTypePlugin:
        # necessary for when the user has passed in a plugin
        if hasattr(training_type, 'parallel_devices') and getattr(training_type, 'parallel_devices') is None:
            training_type.parallel_devices = self.parallel_devices
            if hasattr(training_type, 'num_processes'):
                training_type.num_processes = len(self.parallel_devices)

        if hasattr(training_type, 'cluster_environment') and getattr(training_type, 'cluster_environment') is None:
            training_type.cluster_environment = self.select_cluster_environment()

        if hasattr(training_type, 'num_nodes'):
            # set num_nodes for training_type from trainer setting
            training_type.num_nodes = self.num_nodes

        if hasattr(training_type, 'sync_batchnorm'):
            # set sync_batchnorm for training_type from trainer setting
            training_type.sync_batchnorm = self.sync_batchnorm

        return training_type

    def select_accelerator(self) -> Accelerator:
        if isinstance(self.distributed_backend, Accelerator):
            # custom accelerator from user
            if self._precision_plugin is not None or self._training_type_plugin is not None:
                # plugins also specified by user
                rank_zero_warn(
                    'Specified `Precision` and `TrainingType` plugins will be ignored,'
                    ' since an `Accelerator` instance was provided.'
                )
            return self.distributed_backend

        if self.on_gpu:
            acc_cls = GPUAccelerator
        elif self.on_tpu:
            acc_cls = TPUAccelerator
        elif self.on_ipu:
            acc_cls = IPUAccelerator
        else:
            acc_cls = CPUAccelerator
        # as precision_plugin is dependent on training_type_plugin, make sure
        # that we first select training_type_plugin, then precision_plugin
        return acc_cls(
            training_type_plugin=self.training_type_plugin,
            precision_plugin=self.precision_plugin,
        )

    def select_cluster_environment(self) -> ClusterEnvironment:
        if self._cluster_environment is not None:
            return self._cluster_environment
        if self.is_slurm_managing_tasks:
            env = SLURMEnvironment()
        elif TorchElasticEnvironment.is_using_torchelastic():
            env = TorchElasticEnvironment()
        elif KubeflowEnvironment.is_using_kubeflow():
            env = KubeflowEnvironment()
        else:
            env = LightningEnvironment()
        return env

    def set_distributed_mode(self, distributed_backend: Optional[str] = None):

        if distributed_backend is None and self.is_training_type_in_plugins:
            return

        if distributed_backend is not None and distributed_backend in TrainingTypePluginsRegistry:
            self.distributed_backend = TrainingTypePluginsRegistry[distributed_backend]["distributed_backend"]
        elif distributed_backend is not None:
            self.distributed_backend = distributed_backend

        if isinstance(self.distributed_backend, Accelerator):
            return

        if self.distributed_backend is None:
            if self.has_horovodrun():
                self._set_horovod_backend()
            elif self.num_gpus == 0 and (self.num_nodes > 1 or self.num_processes > 1):
                self._distrib_type = DistributedType.DDP
            elif self.num_gpus > 1:
                rank_zero_warn(
                    'You requested multiple GPUs but did not specify a backend, e.g.'
                    ' `Trainer(accelerator="dp"|"ddp"|"ddp2")`. Setting `accelerator="ddp_spawn"` for you.'
                )
                self.distributed_backend = "ddp_spawn"

        # special case with DDP on CPUs
        if self.distributed_backend == "ddp_cpu":
            self._distrib_type = DistributedType.DDP_SPAWN
            if self.num_gpus > 0:
                rank_zero_warn(
                    'You requested one or more GPUs, but set the backend to `ddp_cpu`. Training will not use GPUs.'
                )
                self.parallel_device_ids = None
            if self.num_processes is None:
                # define the max CPU available
                self.num_processes = os.cpu_count()
        # special case with TPUs
        elif self.distributed_backend == 'tpu' or self.tpu_cores is not None:
            self._device_type = DeviceType.TPU
            if isinstance(self.tpu_cores, int):
                self._distrib_type = DistributedType.TPU_SPAWN
        elif self.distributed_backend == 'ipu':
            self._device_type = DeviceType.IPU
        elif self.distributed_backend and self._distrib_type is None:
            self._distrib_type = DistributedType(self.distributed_backend)

        # unless you request explicitly for CPU and some GPU are available use them
        _on_cpu = self.distributed_backend and 'cpu' in self.distributed_backend
        if self.num_gpus > 0 and not _on_cpu:
            self._device_type = DeviceType.GPU

        _gpu_distrib_types = (DistributedType.DP, DistributedType.DDP, DistributedType.DDP_SPAWN, DistributedType.DDP2)
        # DP and DDP2 cannot run without GPU
        if self.num_gpus == 0 and self._distrib_type in _gpu_distrib_types and not _on_cpu:
            rank_zero_warn(
                'You requested distributed training on GPUs, but none is available, so we set backend to `ddp_cpu`.'
            )
            # todo: in some cases it yield in comparison None and int
            if (self.num_nodes and self.num_nodes > 1) or (self.num_processes and self.num_processes > 1):
                self._distrib_type = DistributedType.DDP
            else:
                rank_zero_warn('You are running on single node with no parallelization, so distributed has no effect.')
                self._distrib_type = None

        # finished configuring self._distrib_type, check ipython environment
        self.check_interactive_compatibility()

        # for DDP overwrite nb processes by requested GPUs
        if (
            self._device_type == DeviceType.GPU
            and self._distrib_type in (DistributedType.DDP, DistributedType.DDP_SPAWN)
        ):
            self.num_processes = self.num_gpus

        if (self._device_type == DeviceType.GPU and self._distrib_type == DistributedType.DDP2):
            self.num_processes = self.num_nodes

        # Horovod is an extra case...
        if self.distributed_backend == "horovod":
            self._set_horovod_backend()

        using_valid_distributed = self.use_ddp or self.use_ddp2
        if self.num_nodes > 1 and not using_valid_distributed:
            # throw error to force user to choose a supported distributed type such as ddp or ddp2
            raise MisconfigurationException(
                'Your chosen distributed type does not support num_nodes > 1. '
                'Please set accelerator=ddp or accelerator=ddp2.'
            )

        rank_zero_info(f'GPU available: {torch.cuda.is_available()}, used: {self._device_type == DeviceType.GPU}')
        num_tpu_cores = self.tpu_cores if self.tpu_cores is not None else 0
        rank_zero_info(f'TPU available: {_TPU_AVAILABLE}, using: {num_tpu_cores} TPU cores')

        num_ipus = self.ipus if self.ipus is not None else 0
        rank_zero_info(f'IPU available: {_IPU_AVAILABLE}, using: {num_ipus} IPUs')

        if torch.cuda.is_available() and self._device_type != DeviceType.GPU:
            rank_zero_warn(
                "GPU available but not used. Set the gpus flag in your trainer"
                " `Trainer(gpus=1)` or script `--gpus=1`."
            )

    def _set_horovod_backend(self):
        self.check_horovod()
        self._distrib_type = DistributedType.HOROVOD

        # Initialize Horovod to get rank / size info
        hvd.init()
        if self.on_gpu:
            # Horovod assigns one local GPU per process
            self.parallel_device_ids = list(range(hvd.local_size()))
        else:
            self.num_processes = hvd.local_size()

    def check_interactive_compatibility(self):
        """
        Raises a `MisconfigurationException` if the accelerator and/or plugin
        is not compatible with an interactive environment
        """
        from pytorch_lightning.utilities import _IS_INTERACTIVE
        if _IS_INTERACTIVE and self._distrib_type is not None and not self._distrib_type.is_interactive_compatible():
            raise MisconfigurationException(
                f"Selected distributed backend {self._distrib_type} is not compatible with an interactive"
                " environment. Run your code as a script, or choose one of the compatible backends:"
                f" {', '.join(DistributedType.interactive_compatible_types())}"
            )

    def check_horovod(self):
        """Raises a `MisconfigurationException` if the Trainer is not configured correctly for Horovod."""
        if not _HOROVOD_AVAILABLE:
            raise MisconfigurationException(
                'Requested `distributed_backend="horovod"`, but Horovod is not installed.'
                "Install with \n $HOROVOD_WITH_PYTORCH=1 pip install horovod[pytorch]"
            )

        if self.num_gpus > 1 or self.num_nodes > 1:
            raise MisconfigurationException(
                "Horovod does not support setting num_nodes / num_gpus explicitly. Use "
                "horovodrun / mpirun to configure the number of processes."
            )

    @staticmethod
    def has_horovodrun() -> bool:
        """Returns True if running with `horovodrun` using Gloo or OpenMPI."""
        return "OMPI_COMM_WORLD_RANK" in os.environ or "HOROVOD_RANK" in os.environ

    def configure_slurm_ddp(self):
        # extract SLURM flag vars
        # whenever we have the correct number of tasks, we let slurm manage processes
        # otherwise we launch the required number of processes
        if self.use_ddp or self.use_ddp2:
            num_requested_gpus = self.num_gpus * self.num_nodes
            num_slurm_tasks = 0
            try:
                num_slurm_tasks = int(os.environ["SLURM_NTASKS"])
                self.is_slurm_managing_tasks = num_slurm_tasks == num_requested_gpus

                # enable slurm cpu
                if num_requested_gpus == 0:
                    self.is_slurm_managing_tasks = num_slurm_tasks == self.num_processes

                # in interactive mode we don't manage tasks
                job_name = os.environ["SLURM_JOB_NAME"]
                if job_name == "bash":
                    self.is_slurm_managing_tasks = False

            except Exception:
                # likely not on slurm, so set the slurm managed flag to false
                self.is_slurm_managing_tasks = False

        # used for tests only, set this flag to simulate slurm managing a task
        try:
            should_fake = int(os.environ["FAKE_SLURM_MANAGING_TASKS"])
            if should_fake:
                self.is_slurm_managing_tasks = True
        except Exception:
            pass

        # notify user the that slurm is managing tasks
        if self.is_slurm_managing_tasks:
            rank_zero_info("Multi-processing is handled by Slurm.")
