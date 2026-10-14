# PDF 去水印工具

简单的本地图形界面：拖入 PDF，点击“去水印并保存”，即可下载清理后的文件。保留命令行模式和高级对象选择。

Python 3.10+，全程本地处理。通过解析 PDF 内容流删除绘制指令，保留正文、头像和原页面尺寸，不将页面转成图片。

参考项目：[Vernalight/remove_codecv_watermark](https://github.com/Vernalight/remove_codecv_watermark)。原项目针对特定 `/Pattern` 绘制序列。本工具独立实现对象筛选和内容流修改，增加透明图片、嵌套 Form、指定 Pattern、Watermark 注释、分页和批量处理，没有复制原项目源码。

## 安装与最短用法

在解压后的工具目录打开终端：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 gui.py
```

Windows 激活命令为 `.venv\Scripts\activate`。只安装 pypdf，无联网上传、OCR 或模型依赖。命令行入口为 `python3 remove_pdf_watermark.py "input.pdf"`，默认输出 `input.clean.pdf`；原文件保持不变。

## 图形界面

完成依赖安装后运行：

```bash
python3 gui.py
```

浏览器会自动打开本地窗口。将一份 PDF 拖入虚线区域，或点击选择文件，然后点击“去水印并保存”。结果默认命名为 `原文件名.clean.pdf`，按浏览器的下载设置保存；也可以点击“再次保存处理结果”重新下载。建议打开结果确认正文完整。

macOS 可双击 `start.command`，Windows 可双击 `start.bat`；两个启动器会优先使用项目中的 `.venv`。文件从 GitHub ZIP 解压后，macOS 若没有执行权限，可运行 `chmod +x start.command`。图形界面使用默认浏览器，没有 Tk、Qt 或拖拽扩展依赖。关闭终端或按 Ctrl+C 停止本地服务。

“高级选项”支持指定页码、图片/Form 资源名、Pattern 名称和 Watermark 注释；“查看识别结果”可显示对象路径及当前选择状态。无可匹配水印时显示说明，不下载空结果。每次处理一份 PDF，最大100 MB；批量处理请用命令行。

服务仅监听 `127.0.0.1`，采用随机会话地址和同源校验。PDF 在本机临时目录处理，请求结束后清理临时文件；网页不会访问第三方服务。此界面为单机工具，不用于部署公开服务器。

## 先检查，再删除

```bash
# 只输出 JSON 对象清单，不修改 PDF
python3 remove_pdf_watermark.py "input.pdf" --inspect

# 查看自动选中的对象；selected=true 表示将删除
python3 remove_pdf_watermark.py "input.pdf" --dry-run

# 自动模式：删除符合结构条件的透明图片层
python3 remove_pdf_watermark.py "input.pdf" -o "clean.pdf"

# 删除已确认的图片或 Form，名称来自 inspect；参数可重复
python3 remove_pdf_watermark.py "input.pdf" --xobject X136 -o "clean.pdf"

# 对象名在不同 PDF 中可能变化，按指纹更方便；至少12位前缀
python3 remove_pdf_watermark.py "input.pdf" --sha256 3c45c6e5b36bfdff76241aee41646f164c3fb5e9cc15baccf6e0b1a1e9311848

# 删除指定 Pattern 的填充；保留同一路径的描边
python3 remove_pdf_watermark.py "input.pdf" --pattern P1

# 删除类型为 /Watermark 的注释，保留链接等其他注释
python3 remove_pdf_watermark.py "input.pdf" --watermark-annots

# 只处理第1页和第3至5页
python3 remove_pdf_watermark.py "input.pdf" --pages 1,3-5 --xobject X136

# 批量处理；输出同名冲突会拒绝执行
python3 remove_pdf_watermark.py "a.pdf" "b.pdf" --output-dir cleaned

# 保存机器可读报告
python3 remove_pdf_watermark.py "input.pdf" --inspect --report objects.json
```

显式使用 `--xobject`、`--sha256`、`--pattern` 或 `--watermark-annots` 时，默认关闭自动筛选；加上 `--auto` 可以同时启用。所有显式选择条件按“或”组合。`--xobject` 和 `--pattern` 在选定页及嵌套 Form 中按局部资源名匹配，同名对象也会匹配；先检查 `page` 和 `path`。指纹描述对象内容及图片解释参数，Form 指纹不包含整个外部资源树。

## 自动模式的适用范围

自动模式检查 Image XObject 的变换面积、8位灰度透明蒙版和跨页重复内容：

- 绘制面积至少为页面积的80%；此面积是变换矩阵的估计，不计算裁剪后的可见范围。
- 透明蒙版最大 alpha 不超过80/255。
- 有色区域占图片面积的0.5%至25%。
- 相同内容出现在至少两页；单页文件允许单次出现。

这是一种结构启发式，不能识别水印文字；符合条件的装饰图片也可能被选中。建议先执行 `--dry-run`。同一指纹的其他调用也会被删除。多页文件只选部分页时，重复次数仍按完整输入文件计算。

## 边界与文件保护

- “通用”指多种可分离 PDF 对象的结构处理，不代表所有 PDF 都能自动去水印。
- Pattern 必须手动指定；不自动删除所有图案，以免误删背景。这里只删除填充，不删除独立的 Pattern 描边。
- 纯文字水印若位于独立 Form，可删除该 Form；普通正文流中的文字水印、矢量路径和内联图片没有专门的自动处理规则。
- 水印已合并进扫描图或照片时，本工具无法恢复被覆盖的像素。
- 删除 Form 会删除其全部内容，应确认里面没有正文；删除指令不等同于敏感信息的彻底擦除，Pattern 或其他未清理的资源仍可能存储于文件中。
- 无匹配对象时不生成新文件，退出码为1；成功或检查模式为0，处理失败为2。批量文件中某一项失败后，继续处理其余文件。
- 默认拒绝覆盖已有 PDF 和报告。`--force` 允许替换输出，仍禁止覆盖输入。输出先写临时文件，再原子发布。
- 加密文件通过环境变量 `PDF_PASSWORD` 提供已有密码；生成结果不继续加密。修改 PDF 会使已有数字签名失效。

## 测试

```bash
python3 -m unittest discover -v
```

测试覆盖自动筛选、指纹匹配、照片保留、分页时的共享资源隔离、嵌套 Form、显式 Form 删除、扫描图不匹配、预览不写文件、输入/输出保护、Pattern 描边、Watermark 注释，以及图形界面的识别、处理、PDF 下载响应、错误提示和本地访问限制。测试自行生成合成 PDF，仓库不包含真实简历。
