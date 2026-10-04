"""极简 JSON Schema 子集校验器的测试。

刻意不依赖 PySide6，也不依赖项目其他模块 —— 只 import 这个 schema 模块，
这样校验器可以被单独测试、单独复用。

运行方式：
    python -m unittest tests.test_tool_schema -v
或
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.core.tools.schema import SchemaError, defaults_from, validate  # noqa: E402


class ValidateHelperMixin:
    """测试里反复用到的两个断言。

    assertInvalid 会返回错误信息，方便进一步断言路径拼接是否正确。
    """

    def assertValid(self, instance: object, schema: dict) -> None:
        self.assertIsNone(validate(instance, schema))

    def assertInvalid(self, instance: object, schema: dict) -> str:
        with self.assertRaises(SchemaError) as ctx:
            validate(instance, schema)
        message = str(ctx.exception)
        self.assertTrue(message, "SchemaError 的 message 不能为空")
        return message


class TypeTests(ValidateHelperMixin, unittest.TestCase):
    """type 关键字。"""

    def test_valid_types(self) -> None:
        cases: list[tuple[object, str]] = [
            ({}, "object"),
            ([], "array"),
            ("abc", "string"),
            (3, "integer"),
            (3.5, "number"),
            (3, "number"),
            (True, "boolean"),
            (False, "boolean"),
            (None, "null"),
        ]
        for instance, type_name in cases:
            with self.subTest(instance=instance, type_name=type_name):
                self.assertValid(instance, {"type": type_name})

    def test_invalid_type_reports_path_and_types(self) -> None:
        message = self.assertInvalid("3", {"type": "integer"})
        self.assertIn("$", message)
        self.assertIn("integer", message)
        self.assertIn("str", message)

    def test_invalid_type_with_object_schema(self) -> None:
        message = self.assertInvalid([1, 2], {"type": "object"})
        self.assertIn("期望 object", message)
        self.assertIn("实际是 list", message)

    def test_bool_is_not_integer(self) -> None:
        # isinstance(True, int) 为 True，但 JSON 里 true 不是整数。
        self.assertInvalid(True, {"type": "integer"})
        self.assertInvalid(False, {"type": "integer"})
        self.assertValid(True, {"type": "boolean"})

    def test_bool_is_not_number(self) -> None:
        self.assertInvalid(True, {"type": "number"})
        self.assertValid(1, {"type": "number"})
        self.assertValid(1.0, {"type": "number"})

    def test_type_union(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertValid("a", schema)
        self.assertValid(None, schema)
        message = self.assertInvalid(1, schema)
        self.assertIn("string", message)
        self.assertIn("null", message)

    def test_type_union_single_member_still_works(self) -> None:
        self.assertValid(2, {"type": ["integer"]})
        self.assertInvalid(2.5, {"type": ["integer"]})

    def test_unknown_type_name_is_ignored(self) -> None:
        # 上游可能传来更丰富的类型名，忽略而不是报错。
        self.assertValid(object(), {"type": "uuid"})


class RequiredTests(ValidateHelperMixin, unittest.TestCase):
    """required 关键字。"""

    def test_required_present(self) -> None:
        self.assertValid({"a": 1}, {"type": "object", "required": ["a"]})

    def test_required_missing(self) -> None:
        message = self.assertInvalid({"b": 1}, {"required": ["a"]})
        self.assertIn("$", message)
        self.assertIn("缺少必需字段", message)
        self.assertIn("a", message)

    def test_required_missing_nested_reports_nested_path(self) -> None:
        schema = {
            "type": "object",
            "properties": {"node": {"type": "object", "required": ["name"]}},
        }
        message = self.assertInvalid({"node": {}}, schema)
        self.assertIn("$.node", message)

    def test_required_ignored_for_non_object(self) -> None:
        self.assertValid("not an object", {"required": ["a"]})


class AdditionalPropertiesTests(ValidateHelperMixin, unittest.TestCase):
    """additionalProperties 关键字。"""

    def test_false_rejects_extra_key(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "additionalProperties": False,
        }
        self.assertValid({"a": 1}, schema)
        message = self.assertInvalid({"a": 1, "extra": 2}, schema)
        self.assertIn("$", message)
        self.assertIn("extra", message)
        self.assertIn("additionalProperties", message)

    def test_false_allows_declared_keys_only(self) -> None:
        schema = {"properties": {"a": {}}, "additionalProperties": False}
        self.assertValid({}, schema)
        self.assertValid({"a": "anything"}, schema)

    def test_default_is_unrestricted(self) -> None:
        schema = {"properties": {"a": {"type": "integer"}}}
        self.assertValid({"a": 1, "b": "whatever"}, schema)

    def test_schema_applies_to_extra_keys(self) -> None:
        schema = {
            "properties": {"a": {"type": "integer"}},
            "additionalProperties": {"type": "string"},
        }
        self.assertValid({"a": 1, "b": "ok"}, schema)
        message = self.assertInvalid({"a": 1, "b": 2}, schema)
        self.assertIn("$.b", message)
        self.assertIn("string", message)


class EnumTests(ValidateHelperMixin, unittest.TestCase):
    """enum 关键字。"""

    def test_enum_hit(self) -> None:
        self.assertValid("mp4", {"enum": ["mp4", "mkv"]})

    def test_enum_miss(self) -> None:
        message = self.assertInvalid("avi", {"enum": ["mp4", "mkv"]})
        self.assertIn("$", message)
        self.assertIn("enum", message)

    def test_enum_bool_is_not_one(self) -> None:
        # True == 1，所以必须先比类型。
        self.assertInvalid(True, {"enum": [1]})
        self.assertInvalid(1, {"enum": [True]})
        self.assertValid(True, {"enum": [True]})

    def test_enum_int_float_equality(self) -> None:
        self.assertValid(1, {"enum": [1.0]})

    def test_enum_in_nested_path(self) -> None:
        schema = {"properties": {"mode": {"enum": ["a", "b"]}}}
        message = self.assertInvalid({"mode": "c"}, schema)
        self.assertIn("$.mode", message)


class NumberBoundsTests(ValidateHelperMixin, unittest.TestCase):
    """minimum / maximum 关键字。"""

    def test_within_bounds(self) -> None:
        schema = {"type": "number", "minimum": 0, "maximum": 10}
        self.assertValid(0, schema)
        self.assertValid(10, schema)
        self.assertValid(5.5, schema)

    def test_below_minimum(self) -> None:
        message = self.assertInvalid(-1, {"minimum": 0})
        self.assertIn("$", message)
        self.assertIn("minimum", message)

    def test_above_maximum(self) -> None:
        message = self.assertInvalid(11, {"maximum": 10})
        self.assertIn("maximum", message)

    def test_bounds_ignored_for_non_numbers(self) -> None:
        self.assertValid("abc", {"minimum": 100})
        self.assertValid([1, 2], {"maximum": 0})


class StringLengthTests(ValidateHelperMixin, unittest.TestCase):
    """minLength / maxLength 关键字。"""

    def test_length_within_bounds(self) -> None:
        schema = {"type": "string", "minLength": 1, "maxLength": 3}
        self.assertValid("a", schema)
        self.assertValid("abc", schema)

    def test_too_short(self) -> None:
        message = self.assertInvalid("", {"minLength": 1})
        self.assertIn("$", message)
        self.assertIn("minLength", message)

    def test_too_long(self) -> None:
        message = self.assertInvalid("abcd", {"maxLength": 3})
        self.assertIn("maxLength", message)

    def test_length_ignored_for_non_strings(self) -> None:
        self.assertValid([1, 2, 3], {"maxLength": 1})
        self.assertValid(12345, {"maxLength": 1})


class ArrayTests(ValidateHelperMixin, unittest.TestCase):
    """数组相关关键字：items / minItems / maxItems。"""

    def test_items_each_validated(self) -> None:
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertValid([1, 2, 3], schema)
        message = self.assertInvalid([1, "x", 3], schema)
        self.assertIn("$[1]", message)
        self.assertIn("integer", message)

    def test_item_count_bounds(self) -> None:
        schema = {"type": "array", "minItems": 1, "maxItems": 2}
        self.assertValid([1], schema)
        self.assertValid([1, 2], schema)

        too_short = self.assertInvalid([], schema)
        self.assertIn("minItems", too_short)

        too_long = self.assertInvalid([1, 2, 3], schema)
        self.assertIn("maxItems", too_long)

    def test_tuple_form_of_items_is_ignored(self) -> None:
        # 只支持单个 schema 形式；tuple 形式被忽略，而不是报错。
        self.assertValid([1, "a"], {"items": [{"type": "integer"}]})

    def test_items_applies_to_arrays_only(self) -> None:
        self.assertValid("not a list", {"items": {"type": "integer"}})


class NestedPathTests(ValidateHelperMixin, unittest.TestCase):
    """嵌套结构与数组下标拼接出的路径必须准确。"""

    def test_nested_object_path(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "video": {
                    "type": "object",
                    "properties": {"width": {"type": "integer"}},
                }
            },
        }
        message = self.assertInvalid({"video": {"width": "1920"}}, schema)
        self.assertIn("$.video.width", message)

    def test_array_index_path(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                }
            },
        }
        instance = {"items": [{"name": "a"}, {"name": "b"}, {"name": 3}]}
        message = self.assertInvalid(instance, schema)
        self.assertIn("$.items[2].name", message)

    def test_nested_array_inside_array_path(self) -> None:
        schema = {"items": {"items": {"type": "integer"}}}
        message = self.assertInvalid([[1], [2, "x"]], schema)
        self.assertIn("$[1][1]", message)

    def test_deep_nesting_path(self) -> None:
        schema = {
            "properties": {
                "a": {"properties": {"b": {"items": {"properties": {"c": {"type": "null"}}}}}}
            }
        }
        message = self.assertInvalid({"a": {"b": [{"c": 1}]}}, schema)
        self.assertIn("$.a.b[0].c", message)

    def test_custom_root_path_is_used(self) -> None:
        with self.assertRaises(SchemaError) as ctx:
            validate(1, {"type": "string"}, path="$.args")
        self.assertIn("$.args", str(ctx.exception))


class UnknownKeywordTests(ValidateHelperMixin, unittest.TestCase):
    """不认识的关键字必须被忽略，而不是报错。"""

    def test_unknown_keywords_ignored(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "帧率",
            "description": "上游传来的更丰富 schema",
            "type": "integer",
            "exclusiveMinimum": 0,
            "multipleOf": 2,
            "pattern": r"^\d+$",
            "format": "int32",
            "examples": [3],
            "x-vendor-extension": {"whatever": True},
        }
        self.assertValid(3, schema)

    def test_unknown_keywords_in_nested_schema_ignored(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "pattern": "^[a-z]+$"}},
        }
        self.assertValid({"name": "ABC"}, schema)

    def test_unknown_keyword_does_not_mask_real_failure(self) -> None:
        message = self.assertInvalid("3", {"type": "integer", "format": "int32"})
        self.assertIn("期望 integer", message)

    def test_empty_schema_accepts_anything(self) -> None:
        for value in (None, 1, "a", [1], {"a": 1}, True):
            with self.subTest(value=value):
                self.assertValid(value, {})

    def test_non_dict_schema_raises(self) -> None:
        with self.assertRaises(SchemaError):
            validate(1, True)  # type: ignore[arg-type]


class DefaultsFromTests(unittest.TestCase):
    """defaults_from：只收集顶层 properties 里带 default 的字段。"""

    def test_collects_top_level_defaults(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "frame_count": {"type": "integer", "default": 30},
                "format": {"type": "string", "default": "mp4"},
                "name": {"type": "string"},
            },
        }
        self.assertEqual(defaults_from(schema), {"frame_count": 30, "format": "mp4"})

    def test_default_none_is_collected(self) -> None:
        schema = {"properties": {"maybe": {"default": None}}}
        self.assertEqual(defaults_from(schema), {"maybe": None})

    def test_empty_cases(self) -> None:
        self.assertEqual(defaults_from({}), {})
        self.assertEqual(defaults_from({"type": "object"}), {})
        self.assertEqual(defaults_from({"properties": {}}), {})
        self.assertEqual(defaults_from({"properties": {"a": {"type": "integer"}}}), {})
        self.assertEqual(defaults_from({"properties": "not-a-dict"}), {})  # type: ignore[dict-item]
        self.assertEqual(defaults_from({"properties": {"a": 1}}), {})  # type: ignore[dict-item]

    def test_only_top_level_is_collected(self) -> None:
        schema = {
            "properties": {
                "nested": {
                    "type": "object",
                    "default": {"x": 1},
                    "properties": {"inner": {"default": 5}},
                }
            }
        }
        self.assertEqual(defaults_from(schema), {"nested": {"x": 1}})

    def test_defaults_do_not_affect_validation(self) -> None:
        schema = {"properties": {"frame_count": {"type": "integer", "default": 30}}}
        # 默认值不参与校验：缺字段不算错，但也不能让类型校验放过错误值。
        self.assertIsNone(validate({}, schema))
        with self.assertRaises(SchemaError):
            validate({"frame_count": "30"}, schema)


if __name__ == "__main__":
    unittest.main()
