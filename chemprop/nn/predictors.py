from abc import abstractmethod

from lightning.pytorch.core.mixins import HyperparametersMixin
import torch
from torch import Tensor, nn
from torch.nn import functional as F

from chemprop.conf import DEFAULT_HIDDEN_DIM
from chemprop.nn.ffn import MLP
from chemprop.nn.hparams import HasHParams
from chemprop.nn.metrics import (
    MSE,
    SID,
    BCELoss,
    BinaryAUROC,
    ChempropMetric,
    CrossEntropyLoss,
    DirichletLoss,
    EvidentialLoss,
    MulticlassMCCMetric,
    MVELoss,
    QuantileLoss,
)
from chemprop.nn.transforms import UnscaleTransform
from chemprop.utils import ClassRegistry, Factory

__all__ = [
    "Predictor",
    "PredictorRegistry",
    "RegressionFFN",
    "MveFFN",
    "EvidentialFFN",
    "BinaryClassificationFFNBase",
    "BinaryClassificationFFN",
    "BinaryDirichletFFN",
    "MulticlassClassificationFFN",
    "MulticlassDirichletFFN",
    "SpectralFFN",
]


class Predictor(nn.Module, HasHParams):
    r"""A :class:`Predictor` is a protocol that defines a differentiable function
    :math:`f` : \mathbb R^d \mapsto \mathbb R^o"""

    input_dim: int
    """the input dimension"""
    output_dim: int
    """the output dimension"""
    n_tasks: int
    """the number of tasks `t` to predict for each input"""
    n_targets: int
    """the number of targets `s` to predict for each task `t`"""
    criterion: ChempropMetric
    """the loss function to use for training"""
    task_weights: Tensor
    """the weights to apply to each task when calculating the loss"""
    output_transform: UnscaleTransform
    """the transform to apply to the output of the predictor"""

    @abstractmethod
    def forward(self, Z: Tensor) -> Tensor:
        pass

    @abstractmethod
    def train_step(self, Z: Tensor) -> Tensor:
        pass

    @abstractmethod
    def encode(self, Z: Tensor, i: int) -> Tensor:
        """Calculate the :attr:`i`-th hidden representation

        Parameters
        ----------
        Z : Tensor
            a tensor of shape ``n x d`` containing the input data to encode, where ``d`` is the
            input dimensionality.
        i : int
            The stop index of slice of the MLP used to encode the input. That is, use all
            layers in the MLP *up to* :attr:`i` (i.e., ``MLP[:i]``). This can be any integer
            value, and the behavior of this function is dependent on the underlying list
            slicing behavior. For example:

            * ``i=0``: use a 0-layer MLP (i.e., a no-op)
            * ``i=1``: use only the first block
            * ``i=-1``: use *up to* the final block

        Returns
        -------
        Tensor
            a tensor of shape ``n x h`` containing the :attr:`i`-th hidden representation, where
            ``h`` is the number of neurons in the :attr:`i`-th hidden layer.
        """
        pass


PredictorRegistry = ClassRegistry[Predictor]()


class _FFNPredictorBase(Predictor, HyperparametersMixin):
    """A :class:`_FFNPredictorBase` is the base class for all :class:`Predictor`\s that use an
    underlying :class:`MLP` to map the learned fingerprint to the desired output.
    """

    _T_default_criterion: ChempropMetric
    _T_default_metric: ChempropMetric

    def __init__(
        self,
        n_tasks: int = 1,
        input_dim: int = DEFAULT_HIDDEN_DIM,
        hidden_dim: int = 300,
        n_layers: int = 1,
        dropout: float = 0.0,
        activation: str | nn.Module = "relu",
        criterion: ChempropMetric | None = None,
        task_weights: Tensor | None = None,
        threshold: float | None = None,
        output_transform: UnscaleTransform | None = None,
    ):
        super().__init__()
        # manually add criterion and output_transform to hparams to suppress lightning's warning
        # about double saving their state_dict values.
        ignore_list = ["criterion", "output_transform", "activation"]
        self.save_hyperparameters(ignore=ignore_list)
        self.hparams["criterion"] = criterion
        self.hparams["output_transform"] = output_transform
        self.hparams["activation"] = activation
        self.hparams["cls"] = self.__class__

        self.ffn = MLP.build(
            input_dim, n_tasks * self.n_targets, hidden_dim, n_layers, dropout, activation
        )
        task_weights = torch.ones(n_tasks) if task_weights is None else task_weights
        self.criterion = criterion or Factory.build(
            self._T_default_criterion, task_weights=task_weights, threshold=threshold
        )
        self.output_transform = output_transform if output_transform is not None else nn.Identity()

    @property
    def input_dim(self) -> int:
        return self.ffn.input_dim

    @property
    def output_dim(self) -> int:
        return self.ffn.output_dim

    @property
    def n_tasks(self) -> int:
        return self.output_dim // self.n_targets

    def forward(self, Z: Tensor) -> Tensor:
        return self.ffn(Z)

    def encode(self, Z: Tensor, i: int) -> Tensor:
        return self.ffn[:i](Z)


@PredictorRegistry.register("regression")
class RegressionFFN(_FFNPredictorBase):
    n_targets = 1
    _T_default_criterion = MSE
    _T_default_metric = MSE

    def forward(self, Z: Tensor) -> Tensor:
        return self.output_transform(self.ffn(Z))

    train_step = forward


@PredictorRegistry.register("regression-mve")
class MveFFN(RegressionFFN):
    n_targets = 2
    _T_default_criterion = MVELoss

    def forward(self, Z: Tensor) -> Tensor:
        Y = self.ffn(Z)
        mean, var = torch.chunk(Y, self.n_targets, 1)
        var = F.softplus(var)

        mean = self.output_transform(mean)
        if not isinstance(self.output_transform, nn.Identity):
            var = self.output_transform.transform_variance(var)

        return torch.stack((mean, var), dim=2)

    train_step = forward


@PredictorRegistry.register("regression-evidential")
class EvidentialFFN(RegressionFFN):
    n_targets = 4
    _T_default_criterion = EvidentialLoss

    def forward(self, Z: Tensor) -> Tensor:
        Y = self.ffn(Z)
        mean, v, alpha, beta = torch.chunk(Y, self.n_targets, 1)
        v = F.softplus(v)
        alpha = F.softplus(alpha) + 1
        beta = F.softplus(beta)

        mean = self.output_transform(mean)
        if not isinstance(self.output_transform, nn.Identity):
            beta = self.output_transform.transform_variance(beta)

        return torch.stack((mean, v, alpha, beta), dim=2)

    train_step = forward


@PredictorRegistry.register("regression-quantile")
class QuantileFFN(RegressionFFN):
    n_targets = 2
    _T_default_criterion = QuantileLoss

    def forward(self, Z: Tensor) -> Tensor:
        lower_bound, upper_bound = torch.chunk(self.ffn(Z), self.n_targets, 1)

        lower_bound = self.output_transform(lower_bound)
        upper_bound = self.output_transform(upper_bound)

        mean = (lower_bound + upper_bound) / 2
        interval = upper_bound - lower_bound

        return torch.stack((mean, interval), dim=2)

    train_step = forward


class BinaryClassificationFFNBase(_FFNPredictorBase):
    pass


@PredictorRegistry.register("classification")
class BinaryClassificationFFN(BinaryClassificationFFNBase):
    n_targets = 1
    _T_default_criterion = BCELoss
    _T_default_metric = BinaryAUROC

    def forward(self, Z: Tensor) -> Tensor:
        Y = super().forward(Z)

        return Y.sigmoid()

    def train_step(self, Z: Tensor) -> Tensor:
        return super().forward(Z)


@PredictorRegistry.register("classification-dirichlet")
class BinaryDirichletFFN(BinaryClassificationFFNBase):
    n_targets = 2
    _T_default_criterion = DirichletLoss
    _T_default_metric = BinaryAUROC

    def forward(self, Z: Tensor) -> Tensor:
        Y = super().forward(Z).reshape(len(Z), -1, 2)

        alpha = F.softplus(Y) + 1

        u = 2 / alpha.sum(-1)
        Y = alpha / alpha.sum(-1, keepdim=True)

        return torch.stack((Y[..., 1], u), dim=2)

    def train_step(self, Z: Tensor) -> Tensor:
        Y = super().forward(Z).reshape(len(Z), -1, 2)

        return F.softplus(Y) + 1


@PredictorRegistry.register("multiclass")
class MulticlassClassificationFFN(_FFNPredictorBase):
    n_targets = 1
    _T_default_criterion = CrossEntropyLoss
    _T_default_metric = MulticlassMCCMetric

    def __init__(
        self,
        n_classes: int,
        n_tasks: int = 1,
        input_dim: int = DEFAULT_HIDDEN_DIM,
        hidden_dim: int = 300,
        n_layers: int = 1,
        dropout: float = 0.0,
        activation: str | nn.Module = "relu",
        criterion: ChempropMetric | None = None,
        task_weights: Tensor | None = None,
        threshold: float | None = None,
        output_transform: UnscaleTransform | None = None,
    ):
        task_weights = torch.ones(n_tasks) if task_weights is None else task_weights
        super().__init__(
            n_tasks * n_classes,
            input_dim,
            hidden_dim,
            n_layers,
            dropout,
            activation,
            criterion,
            task_weights,
            threshold,
            output_transform,
        )

        self.n_classes = n_classes

    @property
    def n_tasks(self) -> int:
        return self.output_dim // (self.n_targets * self.n_classes)

    def forward(self, Z: Tensor) -> Tensor:
        return super().forward(Z).reshape(Z.shape[0], -1, self.n_classes).softmax(-1)

    def train_step(self, Z: Tensor) -> Tensor:
        return super().forward(Z).reshape(Z.shape[0], -1, self.n_classes)


@PredictorRegistry.register("multiclass-dirichlet")
class MulticlassDirichletFFN(MulticlassClassificationFFN):
    _T_default_criterion = DirichletLoss
    _T_default_metric = MulticlassMCCMetric

    def forward(self, Z: Tensor) -> Tensor:
        Y = super().train_step(Z)

        alpha = F.softplus(Y) + 1

        Y = alpha / alpha.sum(-1, keepdim=True)

        return Y

    def train_step(self, Z: Tensor) -> Tensor:
        Y = super().train_step(Z)

        return F.softplus(Y) + 1


class _Exp(nn.Module):
    def forward(self, X: Tensor):
        return X.exp()


@PredictorRegistry.register("spectral")
class SpectralFFN(_FFNPredictorBase):
    n_targets = 1
    _T_default_criterion = SID
    _T_default_metric = SID

    def __init__(self, *args, spectral_activation: str | None = "softplus", **kwargs):
        super().__init__(*args, **kwargs)

        match spectral_activation:
            case "exp":
                spectral_activation = _Exp()
            case "softplus" | None:
                spectral_activation = nn.Softplus()
            case _:
                raise ValueError(
                    f"Unknown spectral activation: {spectral_activation}. "
                    "Expected one of 'exp', 'softplus' or None."
                )

        self.ffn.add_module("spectral_activation", spectral_activation)

    def forward(self, Z: Tensor) -> Tensor:
        Y = super().forward(Z)
        Y = self.ffn.spectral_activation(Y)
        return Y / Y.sum(1, keepdim=True)

    train_step = forward
