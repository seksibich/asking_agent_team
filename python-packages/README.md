# Python 离线依赖包

本目录保存部署用的 Python 依赖包，供 `DEPLOY.md` 在无外网环境下直接安装。

当前包集合面向 **Python 3.11 + Linux x86_64**。如果部署目标是 macOS、ARM 或其他 Python 版本，请重新下载对应平台的包，不要混用这些二进制包。

## 重新生成

在仓库根目录执行以下命令，按目标平台下载全部直接依赖和传递依赖：

```bash
python3 -m pip download --requirement requirements.txt \
  --dest python-packages \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 3.11 \
  --implementation cp \
  --abi cp311
```

下载完成后，在目标 Python 3.11 环境中使用：

```bash
python -m pip install --no-index --find-links=python-packages -r requirements.txt
```
