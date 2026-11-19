"""文档重建服务关键模块骨架。

- sandbox.py   隔离 profile → nsjail 命令（L1/L2）
- streaming.py 千页文档 chunk 流式调度、检查点与重试
- textlayer.py 双层 PDF 组装（图像层 + 不可见文本层，竖排逐字定位）
- validate.py  输出对象树安全扫描 + SSIM/CER 差异量化
"""

__all__ = ["sandbox", "streaming", "textlayer", "validate"]
