# cloud-functions —— EdgeOne Pages 云函数目录（自动生成）

**目录内所有文件均由仓库根目录的 `build_edgeone.py` 生成，请勿手动修改。**

- `[[default]].py` —— 站点根路径全匹配的 Flask 入口（源码来自 `web_app/app.py`）
- `db.py` —— 数据库连接工具（源码来自 `web_app/db.py`）
- `templates/`、`static/` —— 页面模板与样式（源码来自 `web_app/`）
- `requirements.txt` —— 云函数运行时依赖

要改功能，请编辑 `web_app/` 下的源码，然后重新运行：

```bash
python build_edgeone.py
```

部署方式见 `docs/部署到公网_EdgeOne.md`。
