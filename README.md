# 麦麦日常图文空间 · MaiBot Daily Life

读取 [MaiWeave](https://github.com/ilworkcn/MaiWeave) 正在进行的活动，生成第一人称配文与生活插画，通过已登录的 SnowLuma 客户端发布到 QQ 空间。

## 必需依赖：MaiWeave

**本插件依赖 [ilworkcn/MaiWeave](https://github.com/ilworkcn/MaiWeave) 项目，不能脱离 MaiWeave 独立使用。**

使用前，请在**同一个 MaiBot 实例**中安装并启用 MaiWeave，开启其生活引擎，并确保已有有效的当前活动。本插件通过 MaiWeave 提供的只读接口 `ilworkcn.maiweave.get_life_status`（版本 `1`）获取当前活动等生活状态，再生成配文和图片。

本插件不内置 MaiWeave，也不会自动安装它；未安装、未启用、接口不可用或没有有效当前活动时，无法完成当前活动的图文生成与发布。安装和配置方法请参阅 [MaiWeave 项目说明](https://github.com/ilworkcn/MaiWeave)。

**默认关闭自动发布，默认好友可见。** 插件不需要单独登录 QQ 空间、不保存 Cookie。绘图和文本模型可能收费；测试均使用模拟接口，未真实生成付费图片或发布说说。

## 环境要求

| 组件 | 要求 |
| --- | --- |
| Python | 3.11+；本地验证使用 3.12.14，3.11 做语法兼容检查 |
| MaiBot | 1.3.19–1.3.99 |
| 插件 SDK | 2.21.x；开发测试使用 2.21.0 |
| [MaiWeave](https://github.com/ilworkcn/MaiWeave)（必需） | 在同一 MaiBot 实例安装并启用，提供 ilworkcn.maiweave.get_life_status v1 |
| QQ 适配器 | [MaiBot-SnowLuma-Adapter](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter)，实际连接 **SnowLuma** 客户端 |
| 文本模型 | MaiBot 中已配置可用的 utils 任务，或指定其他任务/模型 |
| 绘图服务 | OpenAI 兼容同步图片生成接口，支持 images/generations |

MaiBot 1.4 尚未验证，也未声明支持：调研时上游 SnowLuma 适配器清单只声明兼容至 1.3.99。**不要为了安装而直接修改兼容范围。** NapCat 客户端不支持本插件使用的空间动作；API 名中出现 napcat 只是 SnowLuma 适配器的兼容命名。

## 安装

### ZIP 安装

将发行 ZIP 上传到 MaiBot WebUI 的插件 ZIP 安装入口。安装包根目录直接包含 [plugin.py](plugin.py)、[_manifest.json](_manifest.json) 和 daily_life 模块目录，不包含开发虚拟环境或密钥。

插件 ID：**github.tang-bai.maibot-daily-life**。启用前必须先安装并配置 [MaiWeave](https://github.com/ilworkcn/MaiWeave)，同时准备已连接 SnowLuma 客户端的适配器。

### 手动安装

把项目文件放进 MaiBot 的 plugins/maibot-daily-life 目录，避免重复嵌套目录。在 **MaiBot 根目录**使用 MaiBot 自己的虚拟环境安装依赖：

~~~powershell
uv pip install --python .venv -r plugins/maibot-daily-life/requirements.txt
~~~

不要将本项目的开发用 .venv 复制到 MaiBot。SDK 由 MaiBot 提供，不要用本项目的开发依赖强行替换 Host SDK。首次加载时 MaiBot 根据配置模型生成插件配置；优先在 WebUI 编辑，可参考 [config.example.toml](config.example.toml)。

## 最小配置：先预览，再开启自动发布

1. **plugin.admin_qqs**：填自己的 QQ 号字符串列表。默认空白名单，所有聊天命令均拒绝执行；也会阻止收费生成。
2. **character.appearance**：填写固定外貌、发型、服装。按需要填写画风 art_style、说话语气 voice。这里是本插件的人设，不会自动读取聊天历史或全局人设。
3. **image.base_url**：例如 https://你的服务/v1，不要加 /images/generations。实际请求为 base_url + /images/generations。
4. **image.model**：填服务商实际支持的绘图模型，不是文本模型。size 默认 1024x1024；接口不支持此字段时改为空字符串。
5. **image.api_key**：在 WebUI 的“绘图接口密钥”密码框中直接填写密钥并保存，或在插件配置中填写同名字段。不要把 Key 发到聊天、截图或提交到仓库。
6. 保持 trigger.auto_enabled=false，用管理员账号在 QQ 发送 /daily status，然后 /daily preview。
7. 检查预览图文；需要真实发布测试帖时，明确执行返回的 /daily publish 草稿ID。成功必须返回真实 tid。
8. 验证后在 WebUI 将 trigger.auto_enabled=true。重新加载后首次活动只作基线，等下一项活动变化，**不会立即发当前活动**。

插件配置示例（请将占位值替换为自己的服务和新密钥，不要分享填写后的配置）：

~~~toml
[image]
base_url = "https://你的绘图服务/v1"
api_key = "在这里填写你的新密钥"
model = "服务商支持的绘图模型"
size = "1024x1024"
~~~

Windows、Linux、系统服务和容器均使用同一配置方式。无需环境变量或 .env 文件，也不会从它们读取密钥。API Key 是绘图服务密钥，不是 QQ Token。**密码框只遮挡显示，配置文件中的密钥并未加密**，请保护配置文件及备份。

## 管理员命令

仅接受 QQ 平台且发送者位于管理员白名单的消息，身份取自 Host 可信消息元数据，不接受命令参数伪造。

| 命令 | 行为 |
| --- | --- |
| /daily status | 只读检查配置、MaiWeave、SnowLuma、今日额度及最近任务；不绘图、不发空间 |
| /daily preview | 为当前活动生成草稿，把配文和图片发到当前聊天；**不发空间**，不限本插件冷却和每日次数，每次生成会记账且可能收费 |
| /daily post | **一键生成并直接发空间**：为当前活动生成配文和图片，保存草稿后立即发布；不限本插件冷却和每日次数，最终回执发回当前聊天 |
| /daily publish 草稿ID | 明确发布仍有效的已生成草稿；不重新绘图、不再次计生成额度 |
| /daily retry 任务ID | **修正错误后显式重新生成并发布**；仅限当前活动的生成失败且从未提交空间的任务，不限本插件冷却和每日次数，每次重试记录且可能再次收费 |

同一活动已预览生成草稿时，/daily post 不会重新生成或隐式发布旧草稿，请改用 /daily publish 草稿ID；已经发布、失败或结果不明的活动也不会重复生成。/daily post 是明确的对外发布操作，不需要开启自动发布，也不等待自动触发的稳定窗口。

草稿默认自开始生成起有效 15 分钟，活动结束/变化会提前失效；过期、已提交或结果不明的草稿不能重复发布。建议在私聊预览，避免将草稿发到不必要的群聊。耗时操作后台执行；为兼容群防循环插件，不提前发送受理消息，只回复最终结果。主程序日志中的“命令执行成功”只表示命令已调度，不代表图片生成或空间发布成功。

## 绘图失败和群回复被拦截的排查

1. 先私聊发送 /daily status，查看任务ID、保存的错误、当前额度和发送诊断。群里出现 group-loop-guard、duplicate_outgoing 或 cooldown 说明聊天回复被第三方 Hook 拦截，不是绘图 API 的失败原因。
2. 绘图 HTTP 400/422：检查模型、尺寸和 images/generations 协议；401：检查密钥；403：检查权限；404：检查接口地址和模型；429：检查频率、额度和余额；5xx：检查服务商状态。超时、证书和网络连接失败也会分别提示。
3. 修正接口配置后，用 /daily retry 任务ID 显式重试。已经发布、正在提交、结果不明、曾经提交失败或不属于当前活动的任务不会重试；已有草稿应使用 /daily publish 草稿ID。
4. 插件的“自动生成冷却时间”默认 **7200 秒**，与群防循环的 **120 秒** 是两套独立限制。管理员手动命令已跳过本插件冷却和每日次数限制，无需调小配置即可立即手动重试；自动任务仍限流。每次尝试可能收费，不要删除去重数据或反复盲试。
5. 第三方群防循环仍可拦截短时间重复命令的结果，插件不会绕过它。请等待对应冷却，或在未受该群规则限制的私聊里查看；如私聊也拦截，按防循环插件文档配置管理员/指定会话的例外。

## 自动发布规则

- 每 60 秒读取一次状态，按“活动 ID + 开始时间”识别变化；活动首次被观察到后持续稳定 120 秒才触发。
- 心情、精力、活动标题、scheduled/active 状态更新不算新活动。
- 睡眠、空闲、生活引擎关闭、状态未就绪不自动发布；睡眠中被唤醒也默认不发。
- 同一活动最多自动尝试一次；自动任务默认检查 2 小时生成间隔与每日 3 次上限，按 Asia/Shanghai 计数。管理员手动命令不受这些限制。
- **统计按生成尝试而非成功说说计数**：自动和手动生成、预览、失败及显式重试都记录，自动任务按全部记录检查限流；手动命令即使超过上限也可执行。不会自动重试或退回历史尝试记录。
- 活动首次出现时正处于冷却、当日限额或忙态，则直接跳过，不等条件解除后补发；检查失败会重新累计稳定时长。
- 首次读取只建立基线；重启不补发。启动基线的去重单独持久化，不影响管理员明确预览该活动。
- 绘图前及提交前再次核验活动；活动已经结束、改变、进入睡眠或主开关关闭时丢弃结果。
- 一次一图一文，图片失败不降级为纯文字。任务串行，数据库事务、唯一约束及租约保护并发和热重载。

## 绘图接口合同

基础请求字段为 model、prompt、n=1，以及非空的 size。使用插件配置 image.api_key 作为 Bearer 密钥，不强制传 response_format 等可选参数。

支持同步响应：

~~~json
{"data": [{"b64_json": "PNG或JPEG的标准base64"}]}
~~~

或：

~~~json
{"data": [{"url": "https://公网图片地址/example.png"}]}
~~~

暂不支持参考图、图片编辑、供应商私有异步任务协议或从聊天接口文本里提取图片。若接口返回 WebP、仅支持特殊参数或私有协议，需要先做相应适配。

默认单图最多 8 MiB、16777216 像素，仅静态 PNG/JPEG；这些是**插件自设上限，不是 QQ 官方限制**。绘图请求和下载共享总超时，默认 180 秒。

管理员显式配置的 API 可以是本地 HTTP 服务，但只应填写可信地址；远程生产环境建议 HTTPS。返回的图片 URL 是不可信输入：仅允许公网 HTTP/HTTPS、对应默认端口、最多三次重定向；校验所有 DNS 地址及实际文件格式，拒绝内网、回环、链路本地地址。下载图片不携带绘图 API 密钥、Cookie，也不使用系统代理。若本地绘图服务返回内网图片链接，请改为 b64_json，不要关闭这些保护。

## QQ 空间发布与异常处理

通过 SDK 调用：

~~~text
maibot-team.snowluma-adapter.adapter.napcat.qzone.send_qzone_msg
version="1"
params={content: 配文, images: ["base64://..."], ugc_right: 4}
~~~

可见性：1 公开、4 好友、64 仅自己。默认好友，不自动扩大范围。

- 只在明确成功且得到非空 tid 时记录成功。晚到的有效回执可以将本地“结果不明”更正为成功，不再次提交。
- 发布超时、断连、缺少 tid 或提交期间进程退出，都可能已经发出：标记 uncertain，**不自动重发**，先检查真实空间。
- 如果已取得 tid 但本地数据库保存失败，会明确提示“空间返回成功，但本地记录保存失败”，请保留 tid 人工核对，勿重发。
- SnowLuma 明确拒绝时记录失败。QQ 已登录不代表空间会话和权限始终正常，账号风控/权限不足可能阻止发布。
- 图文先上传后发布，失败可能产生未关联到说说的已上传图片；插件不删除 QQ 远端资源。
- 配置里的超时是插件等待上限；MaiBot RPC 或适配器可能更早超时，不能通过盲目重试解决。

## 数据、隐私与维护

运行数据使用 Host 分配的 data_dir，通常对应 `data/plugins/github.tang-bai.maibot-daily-life/`：SQLite 保存任务、配文、状态摘要、额度、去重键与 tid；图片缓存在其 images 子目录。该任务数据库不保存 API Key、Cookie 或完整 base64；API Key 仅保存在插件配置中，勿分享真实配置或未脱敏备份。

如果此前使用过其他插件 ID，首次启动会把旧目录的任务数据库与草稿图片复制到当前目录（当前目录已有数据库时跳过，多个旧目录取最新），不会移动或删除旧目录，因此去重、额度与草稿不会丢失。

完成图片默认保留 7 天，完成任务明细默认保留 90 天；活跃/有效草稿/结果不明任务的图片不会被清理，uncertain 记录、轻量去重键及额度账本保留以防重复。请自行监控长期数据目录大小。升级和备份保留数据目录；**不要运行中删除数据库来“重置额度”**。

仅向文本模型发送当前活动类型、经保守过滤的活动标题、粗粒度心情和配置的人设；不读取聊天记录，不发送原始地点字段或未来安排。绘图服务接收外貌、画风和生成的场景提示词。标题过滤不能识别所有形式的隐私，人设及活动名称仍应避免真实姓名、住址、联系方式；模型输出也需要自行评估。

禁用自动开关只停止自动生成，管理员仍可预览/明确发布；关闭 plugin.enabled 才阻止新的生成和发布。配置热重载会取消旧后台任务、重新建立活动基线；已提交的请求无法保证远端撤销，请检查结果。

## 开发、测试及打包

在项目根目录创建独立开发环境，不影响 MaiBot：

~~~powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe "maibot-plugin-sdk==2.21.0" -r requirements.txt
.venv/Scripts/python.exe -B -m unittest discover -s tests -q
.venv/Scripts/python.exe -B scripts/build_release.py
~~~

Linux 对应 Python 路径为 .venv/bin/python。打包脚本使用明确文件白名单，排除虚拟环境、真实配置、密钥、数据库和缓存；输出至 dist/，并校验 ZIP 完整性。

离线测试覆盖 SDK 加载、配置、权限、HTTP/DNS 安全、真实 SQLite 并发、活动触发、冷却/跨日边界、预览、取消、重启和不明回执。不访问真实绘图或 QQ 服务。**通过测试不等于你的接口、账号和版本组合已经端到端实测。**

### 源码入口

- [plugin.py](plugin.py)：SDK 生命周期及管理员命令。
- [config.py](daily_life/config.py)：WebUI 配置模型。
- [service.py](daily_life/service.py)：活动监听、草稿和发布编排。
- [images.py](daily_life/images.py)：绘图与受控下载。
- [store.py](daily_life/store.py)：去重、额度、租约和回执持久化。
- [build_release.py](scripts/build_release.py)：可复现 ZIP 打包与校验。

## 参考与许可

本项目独立实现，采用 [MIT License](LICENSE)。通过公开接口连接其他项目，不包含其代码；各上游项目许可独立适用。

- [MaiWeave 状态 API](https://github.com/ilworkcn/MaiWeave/blob/main/docs/只读状态API.md)
- [MaiBot 插件 SDK](https://github.com/Mai-with-u/maibot-plugin-sdk)
- [SnowLuma 适配器 QZone API](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter/blob/main/apis/qzone.py)
- [SnowLuma 图文说说文档](https://snowluma.github.io/zh/docs/api/qzone/send_qzone_msg)
