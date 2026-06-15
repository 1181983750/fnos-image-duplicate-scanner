# Image Duplicate Scanner

Docker 化的图片重复扫描工具，包含 FastAPI 后端和 React 前端。

## 功能

- 扫描指定目录列表下的 `jpg/jpeg/png/webp/jxl`
- JPG 使用 `cjxl --lossless_jpeg=1` 无损转 JXL
- PNG 使用 `cjxl -q 90` 转 JXL
- 计算 SHA256 和 pHash
- 展示 SHA256 或 pHash 完全相同的图片组
- 后端通过 libvips 生成质量 80 的 WebP 缩略图
- 前端显示扫描/转换进度，并支持勾选后批量删除

## 启动

```bash
SCAN_DIR=/path/to/images docker compose up --build
```

打开：

```text
http://localhost:8080
```

后端健康检查默认暴露在：

```text
http://localhost:18000/api/health
```

容器内默认扫描根目录是 `/scan`。前端目录输入框默认填 `/scan`，如果挂载的是多个子目录，可以逐行填写：

```text
/scan/a
/scan/b
```

## 注意

- 删除接口会真实删除宿主机挂载目录里的文件。
- pHash 基于 libvips 读取图片、NumPy DCT 生成，当前按“完全相同”匹配，不做汉明距离近似匹配，避免误报。
- JPG/PNG 转换后会保留原文件，并生成同名 `.jxl` 文件。
