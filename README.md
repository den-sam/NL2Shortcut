# NL2Shortcut

> 一个 0 截屏的桌面自动化工具——用自然语言或热键指挥电脑，绝不偷看你的屏幕。

[![GitHub stars](https://img.shields.io/github/stars/den-sam/NL2Shortcut?style=social)](https://github.com/你的用户名/NL2Shortcut/stargazers)
[![GitHub license](https://img.shields.io/github/license/den-sam/NL2Shortcut)](https://github.com/den-sam/NL2Shortcut/blob/main/LICENSE)
[![爱发电](https://img.shields.io/badge/赞助-爱发电-orange)](https://afdian.com/a/weaefaw)

---

## 📌 为什么做这个项目？

上学期我在证券公司实习，每天要对交易终端做 2000+ 次重复点击。我想用 PyAutoGUI 自动化，结果发现交易终端开启了**防截屏黑屏保护**——截图全是黑的，图像定位直接报废。

PyAutoGUI 的核心范式是“截图 + 图像定位”，这在金融、医疗、央企的合规环境里根本不可行。于是我决定：**做一个永不截图的桌面自动化工具。**

NL2Shortcut 使用 Windows 官方推荐的 `SendInput` API 模拟键盘鼠标事件，**不调用任何截图接口**，因此在防截屏环境下也能稳定运行。

---

## ✨ 核心特性

- **0 截屏**：绝不调用 `BitBlt`、`GetDC` 等截图 API，CI 中硬约束
- **自然语言触发**：按 `Win+Alt+C` 唤出输入框，输入“整理下载文件夹”即可执行
- **SendInput 执行**：微软官方推荐的用户态输入模拟，系统级合法
- **审计日志**：每次操作记录时间、命令、结果，append-only 写入本地 JSON
- **单 EXE 部署**：双击即用，无 Python 运行时依赖
- **私包系统**：YAML 格式定义自动化步骤，可分享、可导入
- **隐私安全**：数据不出域，无外联请求

---

## 🖥️ 快速开始

### 下载

从 [Releases](https://github.com/den-sam/NL2Shortcut/releases) 下载最新版 EXE 文件。

### 使用

1. 双击运行 `NL2Shortcut.exe`，系统托盘出现图标
2. 按 `Win+Alt+C` 唤出输入框
3. 输入“整理下载文件夹”（或其他预设短语），自动执行
4. 查看 `audit.log.jsonl` 了解执行记录

### 自带示例私包

| 私包名称 | 功能 |
|---------|------|
| `file_organizer_daily` | 下载文件夹按扩展名归档 |
| `email_attachment_archiver` | Outlook 附件归档 |
| `excel_data_mover` | Excel 跨表搬运 |
| `tc_file_router` | Total Commander 文件路由 |
| `browser_form_filler` | 浏览器表单填充 |

---

## 🔧 技术原理

### 为什么 0 截屏？

Windows 提供了多种模拟输入的方式：

| 方式 | 可靠性 | 截图依赖 | 合规友好 |
|------|--------|---------|---------|
| `SendInput`（我们选的） | ⭐⭐⭐⭐⭐ | ❌ 无 | ✅ 是 |
| `mouse_event` / `keybd_event` | ⭐⭐（已弃用） | ❌ 无 | ⚠️ 不稳定 |
| `SendMessage` / `PostMessage` | ⭐⭐⭐（部分应用忽略） | ❌ 无 | ⚠️ 易被拦截 |
| PyAutoGUI 截图定位 | ⭐⭐⭐⭐ | ✅ 有 | ❌ 否 |

**SendInput** 是微软官方推荐的用户态输入模拟接口。它将键盘/鼠标事件打包成 `INPUT` 结构体数组，一次性注入系统输入流——走的是和真实物理键盘鼠标完全相同的处理管道。

### 0 截屏的工程约束

- **代码层面**：不调用 `BitBlt`、`GetDC`、`CreateDC`、`PrintWindow`、`DXGI Output Duplication`
- **CI 层面**：Snyk Code 静态扫描，发现截图 API 立即失败
- **架构层面**：执行器只注入事件，不关心屏幕内容

### 当前技术栈

- **语言**：Python 3.10+（v0.1.1）
- **输入模拟**：pyDirectInput（基于 `ctypes` 调用 `SendInput`）
- **GUI**：系统托盘 + 极简输入框（`tkinter`）
- **私包格式**：YAML
- **审计日志**：JSON Lines（append-only）

> ⚠️ **诚实说明**：当前版本是 Python 实现，单任务约 2-3 秒。v0.2 将引入 C++ 执行层，性能提升至 0.5 秒/任务。

---

## 🗺️ 路线图

| 版本 | 时间 | 核心交付 |
|------|------|---------|
| **v0.1.1** ✅ | 2026.7 | Python + pyDirectInput，0 截屏 MVP |
| **v0.2** 🚧 | 2026.8-9 | C++ 执行层（性能 4-6 倍提升）+ L3 LLM 调度 |
| **v0.3** | 2026.10-12 | 等保密评 + SOC 2 + Private 私有化 |
| **v0.4** | 2027.1-6 | 信创适配（UOS ARM / 麒麟 C86） |
| **v0.5** | 2027.7-12 | Rust 执行层 + 编译期强制 0 截屏 |

---

## 🤝 贡献

欢迎任何形式的贡献！包括但不限于：

- 提交 Issue（bug 报告 / 功能建议）
- 编写私包（YAML 格式）
- 改进文档
- 代码贡献（请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)）

### 开发环境
bash

git clone https://github.com/den-sam/NL2Shortcut.git

cd NL2Shortcut

pip install -r requirements.txt

python main.py

纯文本
---

## 📄 许可证

本项目采用 **AGPL-3.0** 许可证。

- **个人非商业使用**：完全免费
- **商业使用**：需购买商业许可（Pro 订阅 ¥19/月 或 Private 私有化部署）
- **修改后分发**：必须开源（AGPL 要求）

---

## ☕ 赞助

我是大二学生，这个项目 80% 的代码是 AI 写的，但每一个架构决策都是我亲手做的。单人开发不易，如果你觉得 NL2Shortcut 帮到了你，欢迎请我喝杯咖啡 ☕

赞助将用于：
- 代码签名证书（¥1,500/年）
- C++ 执行层外包协作
- 知乎推广（让更多人知道这个项目）

[![爱发电](https://img.shields.io/badge/赞助-爱发电-orange)](https://afdian.com/a/weaefaw)
[![GitHub Sponsors](https://img.shields.io/badge/赞助-GitHub%20Sponsors-blue)](https://github.com/sponsors/den-sam)

---

## 🙏 致谢

- 感谢所有在知乎、掘金、V2EX 上给我建议的开发者
- 感谢我的室友和同学帮忙测试
- 特别感谢每一位赞助者——你们的支持让我能继续维护这个项目

---

## 📬 联系方式

- GitHub Issues：https://github.com/den-sam/NL2Shortcut/issues
- 知乎：iTVTr3(https://www.zhihu.com/people/iTVTr3)
- Email：Deng2312025@outlook.com

---

> **“AI 可以写代码，但架构决策必须由人来把关。”**
