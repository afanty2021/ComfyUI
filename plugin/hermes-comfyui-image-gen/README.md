# hermes-comfyui-image-gen

[-Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 `image_gen` provider 插件:把本机 ComfyUI 注册为 `image_generate` 工具的原生后端。无 API key,全程本地,不出网。

## 模型

| model id | 速度 | 适用 |
|---|---|---|
| `qwen21`(默认) | ~4-6 分钟 | 最高质量;图内中英文文字渲染;海报 |
| `zimage` | ~1 分钟 | 快速草稿、构图迭代 |

要求 ComfyUI 的 `models/` 下已链接对应模型文件(diffusion_models / text_encoders / vae),且 ComfyUI 版本支持 Qwen-Image 2.1 与 Z-Image(本仓库 v0.36+)。

## 安装

```bash
# 主 profile
ln -s "$PWD/plugin/hermes-comfyui-image-gen" ~/.hermes/plugins/comfyui-image-gen
# 子 profile(注意:profile 作用域只扫 <profile>/plugins,需单独链接并在该 profile
# 的 config.yaml 里单独写 plugins.enabled)
ln -s "$PWD/plugin/hermes-comfyui-image-gen" ~/.hermes/profiles/<name>/plugins/comfyui-image-gen

hermes plugins enable comfyui-image-gen   # 主 config;子 profile 写其自身 config
hermes gateway restart                    # 插件在 gateway 启动时加载
```

后端选择(per-profile config.yaml):

```yaml
image_gen:
  provider: comfyui
  model: qwen21
```

无终端工具的 profile(如教学 bot)也可用:生图走原生 `image_generate` 工具,在其 `platform_toolsets` 白名单加 `image_gen` 即可,无需开放 terminal。

## 配置(环境变量,均有默认值)

| 变量 | 默认 | 说明 |
|---|---|---|
| `COMFYUI_DIR` | `~/Github/AI-Infra/ComfyUI` | ComfyUI 仓库位置(自动启动时使用) |
| `COMFYUI_PORT` | `8188` | API 端口 |
| `COMFYUI_PYTHON` | `python3` | 自动启动用的解释器(网关解释器未必装有 torch) |
| `COMFYUI_IMAGE_MODEL` | 未设置 | 模型优先级:调用 `model` 参数 > 此变量 > `image_gen.comfyui.model` > `image_gen.model` > `qwen21` |

ComfyUI 未运行时插件会自动以 `$COMFYUI_PYTHON main.py --port <COMFYUI_PORT>` 拉起,日志写 `<COMFYUI_DIR>/comfyui-boot.log`。
