# Desktop组件与源码部署

GA主仓拥有后端与整体分发，独立私有Desktop仓只构建桌面组件。两仓不使用子模块，也不互相复制源码。

## Desktop构建

在Desktop仓手动运行 **Build standalone Desktop components**，选择平台；也可使用 `desktop-component-*` 标签。

| 平台 | 组件产物 | 源码部署位置 |
| --- | --- | --- |
| Windows x64 | `GenericAgent.exe` | `frontends/GenericAgent.exe` |
| macOS ARM64 | `GenericAgent-Desktop-macOS-arm64.zip` | 解压为 `frontends/GenericAgent.app` |
| Linux x64 | `GenericAgent-Linux-x86_64.AppImage` | `frontends/GenericAgent.AppImage` |

UI由Tauri内嵌，不依赖 `frontends/desktop/static/`。组件不包含GA、Python或Python依赖。启动时配置已有GA根目录和Python解释器。

mac组件沿用ad-hoc签名，签名校验失败会中止构建；压缩解压后再次验签。它不是Developer ID签名或Apple公证，首次启动可能需要系统设置中手动允许。当前只构建Apple Silicon，不是Intel或Universal版。保持 `.app` 完整，不修改内部文件。

## GA源码包组装

Windows现有手工/VPS发布流程不变。以下是可选的CI组装入口，不会自动发布或覆盖现有Release：

1. 下载Desktop Actions产物，核对其中记录的Desktop commit、目标平台及SHA256。
2. 将**内层组件文件**与校验和放到现有公开二进制分发渠道。私有仓Actions下载需要认证，且产物只保留30天；不能把Actions外层ZIP或私有下载页当作公开组件地址。
3. 在GA仓选择需要分发的源码版本，手动运行 **Assemble GA source deployment**，填写平台、固定HTTPS组件URL、预期SHA256。
4. 工作流验证哈希，以 `git archive HEAD` 导出当前提交，注入对应 `frontends/` 路径，生成 `GenericAgent-Source-<platform>.zip`（Linux为 `.tar.gz`）。mac最终源码ZIP解压后再次验签。
5. 下载 `ga-source-<platform>` Actions产物。包内 `DESKTOP_COMPONENT.json` 记录GA commit、组件URL与哈希。按原发布流程分发。

这些是源码部署包，不是离线portable运行时或DMG安装器。不会自动下载Python，也不会自动更新用户环境；旧portable标签不再触发本工作流。

## 后端CI

主仓 **GA backend contracts** 测试Python 3.10–3.13，不安装Node/Rust，也不访问Desktop私有仓。两套依赖已移除桌面源码的旧壳/打包契约不再由主仓执行。Desktop仓独立运行前端测试、类型检查、构建及产物检查。
