---
name: hello-workflow
description: 演示如何用 Hello XBC 插件的工具完成一次"探测环境 → 生成问候"的最小流程。当用户想验证工具箱运行时是否正常时使用。
---

# Hello XBC 最小工作流

这个技能用于**验证工具箱运行时是否正常**，本身不完成任何业务。

## 什么时候用

用户说"帮我确认一下工具箱是好的"、"环境正常吗"之类的话时。

## 步骤

1. 调用 `hello_probe`（无参数）。
   - 它是只读工具，不需要授权。
   - 看返回里的 `core_version`、`declared_capabilities`、`ffmpeg_available`、`ai_providers`。

2. 如果 `ffmpeg_available` 为 `false`，说明这台机器没装 FFmpeg 或不在 PATH 上；
   此时**不要**继续尝试任何音视频相关的工具，直接告诉用户缺什么。

3. 如果用户还想要一条问候，再调用 `hello_greet`，参数 `name` 传用户的名字。
   - 它是 `write` 风险工具，**需要用户授权**；未授权时会被内核拒绝，这是预期行为。

## 注意

- 不要调用 `hello_fail`，除非用户明确要求验证错误隔离。
- 这个技能只是示例：它展示的是"技能是说明书，工具才是动作"这个分工。
