"""极简 JSON Schema 子集校验器。

为什么不直接引入 ``jsonschema`` 库？

* 内核坚持"零第三方依赖"：一旦引入第三方包，版本冲突、供应链风险与打包体积
  就一起进了宿主进程，而换来的只是本文件里几百行的能力。
* 工具参数的校验需求很小：类型、必需字段、额外字段策略、枚举、数值/长度/数量
  上下界、数组元素、默认值，仅此而已。
* 上游（插件、模型）可能传来更丰富的 schema，所以本模块对**不认识的关键字一律
  忽略**，而不是报错 —— 向前兼容比"严格拒绝"更重要。

支持的 JSON Schema 关键字（只支持这些，多一个都不支持）：

``type`` / ``properties`` / ``required`` / ``additionalProperties`` /
``enum`` / ``minimum`` / ``maximum`` / ``minLength`` / ``maxLength`` /
``minItems`` / ``maxItems`` / ``items`` / ``default``。

用法::

    validate({"name": "a", "frame_count": 3}, {"type": "object", ...})
    defaults = defaults_from({"properties": {"frame_count": {"default": 0}}})

设计取舍：

* ``items`` 只支持单个 schema 形式，不支持 tuple（数组）形式；后者被忽略。
* ``type`` 中的类型名若不在支持列表内（例如 ``"uuid"``），该约束被忽略。
* ``integer`` 与 ``number`` 都排除 ``bool``：JSON 里 ``true`` 不是数字，
  尽管 Python 中 ``isinstance(True, int)`` 为 True。
* ``enum`` 先比类型再比值，避免 ``True == 1`` 造成的误判。
"""

from __future__ import annotations

__all__ = ["SchemaError", "validate", "defaults_from"]


class SchemaError(ValueError):
    """校验失败。message 必须指出出错的数据路径与原因。"""


def validate(instance: object, schema: dict, path: str = "$") -> None:
    """按 schema 校验 instance。通过则返回 None，不通过则抛 SchemaError。

    参数：
        instance: 待校验的数据。
        schema: JSON Schema 子集（dict）。不支持的关键字被忽略。
        path: 当前数据路径，用于拼接错误信息；调用方通常无需传入。

    异常：
        SchemaError: 校验失败，message 中含数据路径与失败原因。
    """
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema 必须是 dict，实际是 {_type_name(schema)}")

    _check_type(instance, schema, path)
    _check_enum(instance, schema, path)
    _check_number_bounds(instance, schema, path)
    _check_string_length(instance, schema, path)
    _check_array(instance, schema, path)

    if isinstance(instance, dict):
        _check_object(instance, schema, path)


def defaults_from(schema: dict) -> dict:
    """从 schema 收集默认值：顶层 properties 中每个带 "default" 的字段。

    只处理顶层，不下钻嵌套对象；缺少 ``properties``、字段 schema 不是 dict、
    或字段没有 ``default`` 时都不会出现在结果里。``default`` 的值为 ``None``
    同样会被收集（"存在该键"即算有默认值）。
    """
    if not isinstance(schema, dict):
        return {}

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}

    defaults: dict = {}
    for name, sub_schema in properties.items():
        if isinstance(sub_schema, dict) and "default" in sub_schema:
            defaults[name] = sub_schema["default"]
    return defaults


# --------------------------------------------------------------------------- #
# 类型
# --------------------------------------------------------------------------- #

#: 支持的简单类型名。JSON Schema 的 "integer" 与 "number" 需要额外排除 bool。
_SIMPLE_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


def _is_number(value: object) -> bool:
    """JSON 语义下的数字：int/float，且排除 bool（``True`` 不是数字）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _matches_type(value: object, expected: str) -> bool:
    """判断 value 是否满足单个类型名。不认识的类型名一律视为满足（忽略该约束）。"""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _type_name(value: object) -> str:
    """把 Python 值翻译成错误信息里好读的类型名。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _check_type(instance: object, schema: dict, path: str) -> None:
    """校验 type：允许字符串或字符串数组（满足其一即可）。"""
    declared = schema.get("type")
    if declared is None:
        return

    if isinstance(declared, str):
        expected = [declared]
    elif isinstance(declared, (list, tuple)):
        expected = [name for name in declared if isinstance(name, str)]
        if not expected:
            return
    else:
        # 类型声明本身不合法（既不是字符串也不是数组）：忽略，不因此报错。
        return

    if any(_matches_type(instance, name) for name in expected):
        return

    wanted = " 或 ".join(expected)
    raise SchemaError(f"{path}: 期望 {wanted}，实际是 {_type_name(instance)}")


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


def _enum_equal(left: object, right: object) -> bool:
    """按 JSON 语义比较相等：先比类型，规避 ``True == 1`` / ``1 == 1.0`` 的坑。

    数字之间的 1 与 1.0 视为相等（JSON 只有一种 number），但 bool 只与 bool 相等。
    """
    left_is_bool = isinstance(left, bool)
    right_is_bool = isinstance(right, bool)
    if left_is_bool or right_is_bool:
        return left_is_bool and right_is_bool and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        # JSON 只有一种 number：1 与 1.0 视为相等。
        return left == right
    if type(left) is not type(right):
        return False
    return left == right


def _check_enum(instance: object, schema: dict, path: str) -> None:
    """校验 enum：值必须命中列表中的一项。"""
    if "enum" not in schema:
        return

    candidates = schema["enum"]
    if not isinstance(candidates, (list, tuple)):
        # enum 本身不合法：忽略该约束。
        return

    if any(_enum_equal(instance, candidate) for candidate in candidates):
        return

    rendered = "、".join(repr(candidate) for candidate in candidates)
    raise SchemaError(f"{path}: 值 {instance!r} 不在 enum 允许的取值中（允许：{rendered}）")


# --------------------------------------------------------------------------- #
# 数值上下界
# --------------------------------------------------------------------------- #


def _check_number_bounds(instance: object, schema: dict, path: str) -> None:
    """校验 minimum / maximum。只对数字生效，其他类型不受约束。"""
    if not _is_number(instance):
        return

    minimum = schema.get("minimum")
    if _is_number(minimum) and instance < minimum:  # type: ignore[operator]
        raise SchemaError(f"{path}: {instance!r} 小于 minimum {minimum!r}")

    maximum = schema.get("maximum")
    if _is_number(maximum) and instance > maximum:  # type: ignore[operator]
        raise SchemaError(f"{path}: {instance!r} 大于 maximum {maximum!r}")


# --------------------------------------------------------------------------- #
# 字符串长度
# --------------------------------------------------------------------------- #


def _check_string_length(instance: object, schema: dict, path: str) -> None:
    """校验 minLength / maxLength。只对字符串生效。"""
    if not isinstance(instance, str):
        return

    min_length = schema.get("minLength")
    if isinstance(min_length, int) and not isinstance(min_length, bool):
        if len(instance) < min_length:
            raise SchemaError(
                f"{path}: 字符串长度 {len(instance)} 小于 minLength {min_length}"
            )

    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and not isinstance(max_length, bool):
        if len(instance) > max_length:
            raise SchemaError(
                f"{path}: 字符串长度 {len(instance)} 大于 maxLength {max_length}"
            )


# --------------------------------------------------------------------------- #
# 数组
# --------------------------------------------------------------------------- #


def _check_array(instance: object, schema: dict, path: str) -> None:
    """校验数组相关的 minItems / maxItems / items。只对数组生效。"""
    if not isinstance(instance, list):
        return

    min_items = schema.get("minItems")
    if isinstance(min_items, int) and not isinstance(min_items, bool):
        if len(instance) < min_items:
            raise SchemaError(f"{path}: 数组元素个数 {len(instance)} 小于 minItems {min_items}")

    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and not isinstance(max_items, bool):
        if len(instance) > max_items:
            raise SchemaError(f"{path}: 数组元素个数 {len(instance)} 大于 maxItems {max_items}")

    item_schema = schema.get("items")
    if not isinstance(item_schema, dict):
        # 不支持 tuple 形式（items 为数组）的写法：忽略。
        return
    for index, item in enumerate(instance):
        validate(item, item_schema, f"{path}[{index}]")


# --------------------------------------------------------------------------- #
# 对象
# --------------------------------------------------------------------------- #


def _check_object(instance: dict, schema: dict, path: str) -> None:
    """校验对象相关的 required / properties / additionalProperties。"""
    properties = schema.get("properties")
    declared: dict = properties if isinstance(properties, dict) else {}

    # required：对象必须含有的键。
    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        for name in required:
            if isinstance(name, str) and name not in instance:
                raise SchemaError(f"{path}: 缺少必需字段 {name!r}")

    # properties：递归下钻校验。
    for name, sub_schema in declared.items():
        if name in instance:
            validate(instance[name], sub_schema, f"{path}.{name}")

    # additionalProperties：false 时禁止未声明键；dict 时按该 schema 校验未声明键。
    additional = schema.get("additionalProperties")
    if additional is False:
        extras = [name for name in instance if name not in declared]
        if extras:
            rendered = "、".join(repr(name) for name in extras)
            raise SchemaError(
                f"{path}: 出现未在 properties 中声明的字段 {rendered}"
                f"（additionalProperties 为 false）"
            )
    elif isinstance(additional, dict):
        for name, value in instance.items():
            if name not in declared:
                validate(value, additional, f"{path}.{name}")
