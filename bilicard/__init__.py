"""BiliCard 插件核心包。

模块划分：
- parser:       从文本中提取 B站视频标识（BV / av / b23 短链）
- wbi:          B站 WBI 接口签名工具
- client:       B站数据抓取（视频信息、在线人数、热门评论、字幕、UP主投稿）
- summarizer:   字幕 -> LLM 视频总结
- render:       组装模板数据、加载 HTML 模板
- data_manager: 订阅数据存储（基于配置项读写）
- login:        B站扫码登录（二维码生成与轮询）
- credential:   登录凭证持久化（credential.json，原子写）
- config:       配置统一访问（默认值集中）
- log:          日志适配（框架内走 astrbot logger，脱框架回退标准 logging）
"""
