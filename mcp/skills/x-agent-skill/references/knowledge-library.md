# 精致清单：正文与评论组合为发布素材

## 执行边界

用户要求“精致清单、知识库、逐帖分析、沉淀素材、痛点组合、保存 MD”时使用此流程。
只建结构时调用 rebuild_research_index，不生成假笔记或真实采集历史。
实际分析先查已有档案与采集覆盖；目标不明确时追问，不自动复用某位演示博主。
后台只负责数据积累，生成分析和保存报告由当前 Agent 在用户要求时完成，不创建定时模型任务。

服务使用 mcp_knowledge_base_dir 作为唯一根目录。查询返回 library_root；不得凭技能位置猜测路径。
本机可以交付该目录中的文件链接。远程服务返回的是服务器路径，不能称为 Agent 电脑的本地文件。
用户要求不同保存位置时，在已授权主机配置这个字段，重连后核对，不发送不存在的 root 工具参数。

## 文件与身份约定

原始帖子、评论、版本和快照保留在 PostgreSQL；Markdown 是派生研究知识。
博主用数字 ID，改名不新建目录；帖子 ID 唯一，不按昵称或标题重复保存。

| 类型 kind | 固定位置 | 用途 |
| --- | --- | --- |
| post | 博主／数字 ID／帖子／post_帖子ID.md | 每篇已采集帖子的分析 |
| daily | 博主／数字 ID／日报／YYYY-MM-DD.md | 本地自然日的帖子和组合导航 |
| material | 素材／笔记ID.md | 可复用的事实、案例、结构、方法 |
| need | 需求／笔记ID.md | 评论痛点、场景、证据和验证问题 |
| resource | 资源／笔记ID.md | 工具、仓库、教程及官方核验情况 |
| combination | 组合／笔记ID.md | 正文依据＋评论痛点＋资源的发布方案 |
| report | 报告／笔记ID.md | 本期选择清单与行动决策 |

post ID 必须为 post_<数字帖子ID>，daily ID 必须为 daily_<博主ID>_<YYYY-MM-DD>。
其他 ID 使用小写英文／数字／下划线／连字符的稳定 ID；同一素材不因标题改写新建一份。
先检索已有需求与资源，相同资源可按规范化来源 URL 的 SHA-256 前缀构造 resource_<指纹>。
不要用短暂评分、互动数字或当前 handle 作为唯一键。

## 生成与保存步骤

1. query_research_notes 查已存记录；query_author_archives／query_author_data／query_comments 提供原始证据。
   按请求范围遍历所有已采集帖子，为每篇生成卡片，包括不值得借鉴的简短理由，不只选高互动帖。
2. 逐帖区分正文事实、方法／结构解读、可沉淀素材和评论痛点。原始评论提供可追溯的 ID 和链接，
   同一账号多次表达不重复计数；博主自评、重复推广与独立需求分开记录。
3. 按主题聚合素材、需求与资源。官方核验未做则写“未核验”，没有评论则写“尚未采集评论”，
   不能说无需求。词频不是需求结论，高互动不是借鉴效果的因果证明。
4. 组合可跨多个帖子或博主，但必须有可说明的语义关系。每个组合明确：目标读者和问题、正文依据、
   可用资源、切入角度、内容大纲、差异化方式、验证动作、采用理由和不确定性。
   同一素材可服务教程、案例复盘、对比指南等不同组合；按证据相关性筛选，不机械做笛卡尔积。
5. 使用 assets/knowledge-templates 的对应模板，转成 ResearchNote 参数。content 只传正文；
   frontmatter 由 MCP 写入，不能把模板占位符当作真实内容。sources 使用真实平台 ID 和 HTTPS 链接。
   尚缺评论证据的组合必须标记 pending／partial，不可标为 verified。
6. 先保存帖子、素材、需求、资源，再保存引用它们的组合与日报／报告。
   related_note_ids 必须指向已存在笔记；自引用不能替代实际证据。
   save_research_note 成功后核对 content_hash、relative_path 和 index_current；有 warning 必须解释并处理。
7. 同一内容重复保存直接复用。更新前 get_research_note 取当前内容和 hash，合并用户手工改动后，
   带 expected_hash 更新；KB_CONFLICT 时重新读取，不能去掉 hash 强行覆盖。
   旧版本位于 .history；用户选择／发布状态不得因为重做分析被重置。
   保存前会再次检查文件，但外部编辑器不遵守 MCP 写锁。不得同时手工编辑和让 MCP 写同一笔记；
   极短的文件替换窗口无法保证阻止外部编辑。冲突时保留现有文件并重新合并，不自动覆盖。
8. 帖子的 date 是用户时区的发布时间日期，日报同口径；缺发布时间时不猜日期，先补充或说明缺口。
   报告 date 为报告日期，范围在正文声明；前后采集时间单独说明。
   每日文件只在本次生成／更新，不意味着已有后台自动写报告。

## 元数据与索引

所有笔记含：id、kind、title、date、timezone、author_ids、tags、source_post_ids、source_comment_ids、
source_urls、related_note_ids、evidence_status、comment_coverage、status、summary，以及系统 created_at／updated_at。
状态为 draft／reviewed／selected／published／archived；“published”仅记录用户已确认的发布事实，不执行发布。
evidence_status 表示引用证据核对情况，verified 不代表所有评论完整或商业需求已被证明。

保存后自动生成 INDEX.md 与 index.json，按日期、博主、类型、主题检索。
query_research_notes 支持 kind／author_id／since／until／tag／text／limit／cursor；日期 start-inclusive、end-exclusive。
text 只匹配标题、摘要和标签，当前没有向量检索或 Markdown 全文搜索。分页保持筛选条件不变。
手工改动笔记后用 rebuild_research_index；它只重建导航，不改笔记正文。
frontmatter 使用每行一个字段、字段值为 JSON 的 YAML 子集；保留引号与单行数组，不改成多行属性格式。
可在 Obsidian 中阅读与编辑正文；当前未支持其属性编辑器改写的所有 YAML 格式。content 只在正文中，不能加到 frontmatter。
索引异常先修 frontmatter，避免静默漏掉卡片；模板目录和 .history 不计入正式索引。
写锁 .write.lock 记录 PID 与开始时间，正常完成自动删除；崩溃后不会自动抢锁。
若持续 KB_BUSY，先停止所有知识库写入，确认所属进程已退出且无其他写入者，再人工移除失效锁并重建索引。
笔记中禁止保存 token、数据库口令、API 密钥等凭据；校验拦截常见格式，Agent 仍须在保存前检查正文与来源。

## 默认选题数量

config.json 的 knowledge_library.combination_target 默认是 5，表示 Agent 最多推荐 5 个有依据的组合，
并非采集量或 MCP 参数。用户指定数量优先；证据不足时少推荐并说明缺口，不为凑数编造需求。
已保存的数据优先复用，只有用户要求补采时才创建采集任务。

## 交付与后续复用

交付实际可打开的 INDEX.md、日报和组合报告链接，说明本次新增／更新笔记数、覆盖范围、评论缺口。
后续可按“上次已选组合”“某主题需求”“哪些素材还没用过”检索，再读取具体笔记组合。
可链接到 Obsidian 等 Markdown 知识库，但不要声称已安装软件、创建向量库或完成自动发布。
真实笔记、评论摘录、报告、个人偏好只保留本地，不随功能代码推送公开仓库。
