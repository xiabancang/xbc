"""TTS 引擎实现目录。

## 约定（只有一条）

每个引擎一个文件，文件名 = 配置里 `voice_tts.engine` 的值，模块里暴露一个工厂：

```python
def create_provider() -> ModelProvider:
    ...
```

加一个新引擎 = **加一个文件 + 改一行配置**，`plugin.py` 不用动。

## 为什么没有 `__init__.py` 之外的 import

插件加载器（`manager._import_module`）只把**插件根目录**临时放进 `sys.path`，
且函数返回时立刻移除。所以：

- `import providers.moss_tts` 会把 `providers` 以**顶层包名**永久留在 `sys.modules`；
- `providers` 是个很通用的名字 —— 别的插件若有同名目录，就会静默拿到**我们的**模块。

因此 `plugin.py` **按文件路径加载**（`importlib.util.spec_from_file_location`），
模块名带插件前缀（`xbc_voice_tts_provider_<engine>`），互不干扰。
本目录**不作为 Python 包被导入**。

## 职责边界

本目录下的文件是**模型实现**，允许 import 本地推理运行时
（`onnxruntime` / `soundfile` / `soxr` / …）。
插件根目录的 `plugin.py` 是**模型使用者**，只能走 `ctx.ai.*`。
依据：TASK-010 §7 细化后的《AI 能力隔离》。
"""
