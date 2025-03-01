#   Copyright 2020 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
import warnings

from collections.abc import Mapping
from functools import singledispatch
from typing import Dict, Optional, Union

import aesara.tensor as at
import numpy as np

from aeppl import factorized_joint_logprob
from aeppl.transforms import TransformValuesOpt
from aesara import config
from aesara.graph.basic import graph_inputs, io_toposort
from aesara.graph.op import Op, compute_test_value
from aesara.tensor.random.op import RandomVariable
from aesara.tensor.subtensor import (
    AdvancedIncSubtensor,
    AdvancedIncSubtensor1,
    AdvancedSubtensor,
    AdvancedSubtensor1,
    IncSubtensor,
    Subtensor,
)
from aesara.tensor.var import TensorVariable

from pymc.aesaraf import extract_rv_and_value_vars, floatX, rvs_to_value_vars


@singledispatch
def logp_transform(op: Op):
    return None


def _get_scaling(total_size, shape, ndim):
    """
    Gets scaling constant for logp

    Parameters
    ----------
    total_size: int or list[int]
    shape: shape
        shape to scale
    ndim: int
        ndim hint

    Returns
    -------
    scalar
    """
    if total_size is None:
        coef = floatX(1)
    elif isinstance(total_size, int):
        if ndim >= 1:
            denom = shape[0]
        else:
            denom = 1
        coef = floatX(total_size) / floatX(denom)
    elif isinstance(total_size, (list, tuple)):
        if not all(isinstance(i, int) for i in total_size if (i is not Ellipsis and i is not None)):
            raise TypeError(
                "Unrecognized `total_size` type, expected "
                "int or list of ints, got %r" % total_size
            )
        if Ellipsis in total_size:
            sep = total_size.index(Ellipsis)
            begin = total_size[:sep]
            end = total_size[sep + 1 :]
            if Ellipsis in end:
                raise ValueError(
                    "Double Ellipsis in `total_size` is restricted, got %r" % total_size
                )
        else:
            begin = total_size
            end = []
        if (len(begin) + len(end)) > ndim:
            raise ValueError(
                "Length of `total_size` is too big, "
                "number of scalings is bigger that ndim, got %r" % total_size
            )
        elif (len(begin) + len(end)) == 0:
            return floatX(1)
        if len(end) > 0:
            shp_end = shape[-len(end) :]
        else:
            shp_end = np.asarray([])
        shp_begin = shape[: len(begin)]
        begin_coef = [floatX(t) / shp_begin[i] for i, t in enumerate(begin) if t is not None]
        end_coef = [floatX(t) / shp_end[i] for i, t in enumerate(end) if t is not None]
        coefs = begin_coef + end_coef
        coef = at.prod(coefs)
    else:
        raise TypeError(
            "Unrecognized `total_size` type, expected int or list of ints, got %r" % total_size
        )
    return at.as_tensor(floatX(coef))


subtensor_types = (
    AdvancedIncSubtensor,
    AdvancedIncSubtensor1,
    AdvancedSubtensor,
    AdvancedSubtensor1,
    IncSubtensor,
    Subtensor,
)


def logpt(
    var: TensorVariable,
    rv_values: Optional[Union[TensorVariable, Dict[TensorVariable, TensorVariable]]] = None,
    *,
    jacobian: bool = True,
    scaling: bool = True,
    transformed: bool = True,
    sum: bool = True,
    **kwargs,
) -> TensorVariable:
    """Create a measure-space (i.e. log-likelihood) graph for a random variable
    or a list of random variables at a given point.

    The input `var` determines which log-likelihood graph is used and
    `rv_value` is that graph's input parameter.  For example, if `var` is
    the output of a ``NormalRV`` ``Op``, then the output is a graph of the
    density function for `var` set to the value `rv_value`.

    Parameters
    ==========
    var
        The `RandomVariable` output that determines the log-likelihood graph.
        Can also be a list of variables. The final log-likelihood graph will
        be the sum total of all individual log-likelihood graphs of variables
        in the list.
    rv_values
        A variable, or ``dict`` of variables, that represents the value of
        `var` in its log-likelihood.  If no `rv_value` is provided,
        ``var.tag.value_var`` will be checked and, when available, used.
    jacobian
        Whether or not to include the Jacobian term.
    scaling
        A scaling term to apply to the generated log-likelihood graph.
    transformed
        Apply transforms.
    sum
        Sum the log-likelihood.

    """
    # TODO: In future when we drop support for tag.value_var most of the following
    # logic can be removed and logpt can just be a wrapper function that calls aeppl's
    # joint_logprob directly.

    # If var is not a list make it one.
    if not isinstance(var, list):
        var = [var]

    # If logpt isn't provided values and the variable (provided in var)
    # is an RV, it is assumed that the tagged value var or observation is
    # the value variable for that particular RV.
    if rv_values is None:
        rv_values = {}
        for _var in var:
            if isinstance(_var.owner.op, RandomVariable):
                rv_value_var = getattr(
                    _var.tag, "observations", getattr(_var.tag, "value_var", _var)
                )
                rv_values = {_var: rv_value_var}
    elif not isinstance(rv_values, Mapping):
        # Else if we're given a single value and a single variable we assume a mapping among them.
        rv_values = (
            {var[0]: at.as_tensor_variable(rv_values).astype(var[0].type)} if len(var) == 1 else {}
        )

    # Since the filtering of logp graph is based on value variables
    # provided to this function
    if not rv_values:
        warnings.warn("No value variables provided the logp will be an empty graph")

    if scaling:
        rv_scalings = {}
        for _var in var:
            rv_value_var = getattr(_var.tag, "observations", getattr(_var.tag, "value_var", _var))
            rv_scalings[rv_value_var] = _get_scaling(
                getattr(_var.tag, "total_size", None), rv_value_var.shape, rv_value_var.ndim
            )

    # Aeppl needs all rv-values pairs, not just that of the requested var.
    # Hence we iterate through the graph to collect them.
    tmp_rvs_to_values = rv_values.copy()
    transform_map = {}
    for node in io_toposort(graph_inputs(var), var):
        try:
            curr_vars = [node.default_output()]
        except ValueError:
            curr_vars = node.outputs
        for curr_var in curr_vars:
            rv_value_var = getattr(
                curr_var.tag, "observations", getattr(curr_var.tag, "value_var", None)
            )
            if rv_value_var is None:
                continue
            rv_value = rv_values.get(curr_var, rv_value_var)
            tmp_rvs_to_values[curr_var] = rv_value
            # Along with value variables we also check for transforms if any.
            if hasattr(rv_value_var.tag, "transform") and transformed:
                transform_map[rv_value] = rv_value_var.tag.transform

    transform_opt = TransformValuesOpt(transform_map)
    temp_logp_var_dict = factorized_joint_logprob(
        tmp_rvs_to_values, extra_rewrites=transform_opt, use_jacobian=jacobian, **kwargs
    )

    # aeppl returns the logpt for every single value term we provided to it. This includes
    # the extra values we plugged in above so we need to filter those out.
    logp_var_dict = {}
    for value_var, _logp in temp_logp_var_dict.items():
        if value_var in rv_values.values():
            logp_var_dict[value_var] = _logp

    # If it's an empty dictionary the logp is None
    if not logp_var_dict:
        logp_var = None
    else:
        # Otherwise apply appropriate scalings and at.add and/or at.sum the
        # graphs accordingly.
        if scaling:
            for _value in logp_var_dict.keys():
                if _value in rv_scalings:
                    logp_var_dict[_value] *= rv_scalings[_value]

        if len(logp_var_dict) == 1:
            logp_var_dict = tuple(logp_var_dict.values())[0]
            if sum:
                logp_var = at.sum(logp_var_dict)
            else:
                logp_var = logp_var_dict
        else:
            if sum:
                logp_var = at.sum([at.sum(factor) for factor in logp_var_dict.values()])
            else:
                logp_var = at.add(*logp_var_dict.values())

        # Recompute test values for the changes introduced by the replacements
        # above.
        if config.compute_test_value != "off":
            for node in io_toposort(graph_inputs((logp_var,)), (logp_var,)):
                compute_test_value(node)

    return logp_var


def logcdfpt(
    var: TensorVariable,
    rv_values: Optional[Union[TensorVariable, Dict[TensorVariable, TensorVariable]]] = None,
    *,
    scaling: bool = True,
    sum: bool = True,
    **kwargs,
) -> TensorVariable:
    """Create a measure-space (i.e. log-cdf) graph for a random variable at a given point.

    Parameters
    ==========
    var
        The `RandomVariable` output that determines the log-likelihood graph.
    rv_values
        A variable, or ``dict`` of variables, that represents the value of
        `var` in its log-likelihood.  If no `rv_value` is provided,
        ``var.tag.value_var`` will be checked and, when available, used.
    jacobian
        Whether or not to include the Jacobian term.
    scaling
        A scaling term to apply to the generated log-likelihood graph.
    transformed
        Apply transforms.
    sum
        Sum the log-likelihood.

    """
    if not isinstance(rv_values, Mapping):
        rv_values = {var: rv_values} if rv_values is not None else {}

    rv_var, rv_value_var = extract_rv_and_value_vars(var)

    rv_value = rv_values.get(rv_var, rv_value_var)

    if rv_var is not None and rv_value is None:
        raise ValueError(f"No value variable specified or associated with {rv_var}")

    if rv_value is not None:
        rv_value = at.as_tensor(rv_value)

        if rv_var is not None:
            # Make sure that the value is compatible with the random variable
            rv_value = rv_var.type.filter_variable(rv_value.astype(rv_var.dtype))

        if rv_value_var is None:
            rv_value_var = rv_value

    rv_node = rv_var.owner

    rng, size, dtype, *dist_params = rv_node.inputs

    # Here, we plug the actual random variable into the log-likelihood graph,
    # because we want a log-likelihood graph that only contains
    # random variables.  This is important, because a random variable's
    # parameters can contain random variables themselves.
    # Ultimately, with a graph containing only random variables and
    # "deterministics", we can simply replace all the random variables with
    # their value variables and be done.
    tmp_rv_values = rv_values.copy()
    tmp_rv_values[rv_var] = rv_var

    logp_var = _logcdf(rv_node.op, rv_var, tmp_rv_values, *dist_params, **kwargs)

    transform = getattr(rv_value_var.tag, "transform", None) if rv_value_var else None

    # Replace random variables with their value variables
    replacements = rv_values.copy()
    replacements.update({rv_var: rv_value, rv_value_var: rv_value})

    (logp_var,), _ = rvs_to_value_vars(
        (logp_var,),
        apply_transforms=False,
        initial_replacements=replacements,
    )

    if sum:
        logp_var = at.sum(logp_var)

    if scaling:
        logp_var *= _get_scaling(
            getattr(rv_var.tag, "total_size", None), rv_value.shape, rv_value.ndim
        )

    # Recompute test values for the changes introduced by the replacements
    # above.
    if config.compute_test_value != "off":
        for node in io_toposort(graph_inputs((logp_var,)), (logp_var,)):
            compute_test_value(node)

    if rv_var.name is not None:
        logp_var.name = f"__logp_{rv_var.name}"

    return logp_var


def logp(var, rv_values, **kwargs):
    """Create a log-probability graph."""

    # Attach the value_var to the tag of var when it does not have one
    if not hasattr(var.tag, "value_var"):
        if isinstance(rv_values, Mapping):
            value_var = rv_values[var]
        else:
            value_var = rv_values
        var.tag.value_var = at.as_tensor_variable(value_var, dtype=var.dtype)

    return logpt(var, rv_values, **kwargs)


def logcdf(var, rv_values, **kwargs):
    """Create a log-CDF graph."""

    # Attach the value_var to the tag of var when it does not have one
    if not hasattr(var.tag, "value_var"):
        if isinstance(rv_values, Mapping):
            value_var = rv_values[var]
        else:
            value_var = rv_values
        var.tag.value_var = at.as_tensor_variable(value_var, dtype=var.dtype)

    return logcdfpt(var, rv_values, **kwargs)


@singledispatch
def _logcdf(op, values, *args, **kwargs):
    """Create a log-CDF graph.

    This function dispatches on the type of `op`, which should be a subclass
    of `RandomVariable`.  If you want to implement new log-CDF graphs
    for a `RandomVariable`, register a new function on this dispatcher.

    """
    raise NotImplementedError()


def logpt_sum(*args, **kwargs):
    """Return the sum of the logp values for the given observations.

    Subclasses can use this to improve the speed of logp evaluations
    if only the sum of the logp values is needed.
    """
    return logpt(*args, sum=True, **kwargs)
