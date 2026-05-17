# AGENTS.md

## Hugo 文章生成与整理规则

这个仓库是 Hugo 站点。以后新增、迁移或整理文章时，必须遵守下面的结构和约定。

### 文章目录结构

- 所有文章统一放在 `content/posts/` 下。
- 文章必须按分类分组，例如：
  - `content/posts/linux/`
  - `content/posts/freebsd/`
  - `content/posts/docker/`
  - `content/posts/routeros/`
- 每篇文章必须使用 Hugo leaf bundle 结构：

```text
content/posts/<分类>/<文章-slug>/index.md
content/posts/<分类>/<文章-slug>/assets/
```

- 文章正文文件固定命名为 `index.md`。
- 该文章使用的封面图、截图和相关图片统一放入同级 `assets/` 文件夹。
- 文章正文和 front matter 中的图片路径使用相对路径，例如：

```toml
image = 'assets/cover.png'
```

```markdown
![说明](assets/example.png)
```

- 不要把文章专属图片继续放到 `static/images/<分类>/original/` 这类公共静态目录中。
- `static/images/` 只保留全站共用素材，例如站点 logo、默认封面等。

### 公共文章模板

- 新文章统一使用 `archetypes/default/index.md` 作为公共模板。
- 模板中不要写 `categories` 字段。
- 模板中可以保留 `date`、`draft`、`title`、`description`、`tags`、`image`、`toc` 等通用字段。
- 推荐新建文章命令：

```bash
hugo new content posts/<分类>/<文章-slug>
```

示例：

```bash
hugo new content posts/linux/my-new-post
```

生成后应得到：

```text
content/posts/linux/my-new-post/index.md
content/posts/linux/my-new-post/assets/
```

### 分类级联规则

- 每个分类目录下必须有 `_index.md`，用 Hugo cascade 自动给该分类下的文章添加分类信息。
- 分类 `_index.md` 示例：

```toml
+++
title = "Linux"
date = 2026-02-26T20:00:00+08:00
draft = false
[cascade]
  categories = ["Linux"]
[build]
  list = 'never'
+++
```

- 新增分类时，必须先建立：

```text
content/posts/<分类>/_index.md
```

- 分类名称以 `[cascade].categories` 为准。
- 文章自己的 `index.md` 不再手动写 `categories`，避免重复维护。

### 现有文章迁移规则

- 如果发现旧文章仍是 `content/posts/<分类>/<文章>.md`，需要迁移为：

```text
content/posts/<分类>/<文章>/index.md
```

- 迁移时同步处理文章引用的图片：
  - 将该文章引用的图片复制或移动到该文章的 `assets/` 目录。
  - 将正文中的 `/images/...` 文章专属图片路径改为 `assets/...`。
  - 将 front matter 的 `image` 改为 `assets/...`。
  - 移除文章 front matter 中的 `categories` 字段，让分类继承自分类 `_index.md`。

### 构建验证

- 完成结构调整、文章迁移、模板修改或图片路径修改后，必须运行 Hugo 构建验证：

```bash
hugo --destination /private/tmp/hugo-build --cleanDestinationDir --minify --gc
```

- 构建必须成功，且文章页、分类页、封面图和正文图片路径都应正常。
- 验证构建统一输出到 `/private/tmp/hugo-build` 这类临时目录，不要输出到仓库根目录。
- 不要把 Hugo 生成产物当作源码保留在仓库根目录。

### 源码结构完整性与清理规则

- 这个仓库的 Hugo 源码结构应保持清晰，只保留源码、配置和必要的静态资源。
- 根目录下应保留的源码相关路径主要包括：
  - `archetypes/`
  - `assets/`
  - `content/`
  - `layouts/`
  - `static/`
  - `hugo.yaml`
  - `AGENTS.md`
  - `.github/`
  - `.gitignore`
  - `CNAME`
- 以下属于 Hugo 生成产物或历史输出，不应作为源码保留在仓库根目录：
  - `categories/`
  - `css/`
  - `images/`
  - `js/`
  - `page/`
  - `posts/`
  - `public/`
  - `resources/`
  - `search/`
  - `tags/`
  - `index.html`
  - `index.json`
  - `index.xml`
  - `sitemap.xml`
  - `.hugo_build.lock`
- 如果发现上述生成产物重新出现在根目录，先确认它们不是源码目录后再清理。
- 清理后必须重新运行 Hugo 构建验证，确保源码结构仍可完整构建出站点。

### 代码复制与空白字符规则

- 代码框复制逻辑位于 `assets/js/code-copy.js`。
- Hugo 代码块 render hook 位于 `layouts/_default/_markup/render-codeblock.html`。
- 复制代码时必须保持代码严谨性。
- 不允许在复制结果中自动生成原文以外的任何元素，包括普通空格。
- 如果需要处理 `C2 A0` / `\u00A0` / NBSP 等非源码空白字符，只能剔除这些异常字符，不能替换成普通空格或其他字符。
- 代码块复制必须使用 Hugo 代码块 render hook 中保存的原始 Markdown 代码内容。
- render hook 应将复制按钮、语法高亮 HTML 和原始代码源绑定在同一个 `.code-block` 容器内，原始代码源使用 `.code-source` 保存。
- `assets/js/code-copy.js` 应从同一个 `.code-block` 内读取 `.code-source`，不要从高亮后的 DOM 文本、`innerText` 或 `textContent` 拼接复制内容。
- 只有在没有 `.code-source` 的普通 `<pre>` 代码块上，才允许回退读取 `<pre>` 文本，并且仍需剔除 NBSP 等异常非源码空白字符。
- 远程 SSH 终端中大段 heredoc 粘贴错乱通常属于终端/SSH 交互问题，不应通过放宽代码复制规则来规避；文章应优先提供短命令加编辑器粘贴等更稳妥的导入方式。
