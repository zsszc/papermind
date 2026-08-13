"""上传文件内容验证。

扩展名和 Content-Type 都由客户端声明，不能作为内容安全边界。
本模块只做有界、离线的结构检查，通过后才允许交给 PDF/DOCX 解析器。
"""

import zipfile
from pathlib import Path, PurePosixPath


DOCX_MAX_MEMBERS = 2048
DOCX_MAX_MEMBER_SIZE = 32 * 1024 * 1024
DOCX_MAX_TOTAL_SIZE = 128 * 1024 * 1024
DOCX_MAX_COMPRESSION_RATIO = 100.0
_DOCX_REQUIRED_MEMBERS = frozenset({"[Content_Types].xml", "word/document.xml"})
_VALIDATION_CHUNK_SIZE = 1024 * 1024


class UploadValidationError(ValueError):
    """上传内容不符合安全边界。"""


def validate_pdf(path: Path) -> None:
    """要求文件以 PDF 头部标识开始。

    该门禁用于拒绝明显的扩展名伪装，不替代后续 PDF 解析。
    """
    try:
        with path.open("rb") as stream:
            header = stream.read(5)
    except OSError as exc:
        raise UploadValidationError("无法读取 PDF 文件") from exc
    if header != b"%PDF-":
        raise UploadValidationError("文件内容不是有效 PDF")


def _validate_member_name(name: str) -> None:
    normalized = name.replace("\\", "/")
    member_path = PurePosixPath(normalized)
    if (
        not name
        or "\\" in name
        or member_path.is_absolute()
        or any(part in {"", ".", ".."} for part in member_path.parts)
    ):
        raise UploadValidationError("DOCX 包含不安全的成员路径")


def validate_docx(
    path: Path,
    *,
    max_members: int = DOCX_MAX_MEMBERS,
    max_member_size: int = DOCX_MAX_MEMBER_SIZE,
    max_total_size: int = DOCX_MAX_TOTAL_SIZE,
    max_compression_ratio: float = DOCX_MAX_COMPRESSION_RATIO,
) -> None:
    """在 python-docx 解析前检查 OPC ZIP 结构与解压资源预算。

    中央目录先做快速上限判断，随后分块读取每个成员，让 zipfile 完成
    CRC 校验并用实际解压字节数再次执行单项/总量门禁。
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > max_members:
                raise UploadValidationError("DOCX ZIP 成员数超过上限")

            seen = set()
            declared_total = 0
            for info in infos:
                _validate_member_name(info.filename)
                duplicate_key = info.filename.casefold()
                if duplicate_key in seen:
                    raise UploadValidationError("DOCX ZIP 包含重复成员")
                seen.add(duplicate_key)
                if info.flag_bits & 0x1:
                    raise UploadValidationError("DOCX ZIP 不允许加密成员")
                if info.file_size > max_member_size:
                    raise UploadValidationError("DOCX ZIP 单个成员解压大小超过上限")
                declared_total += info.file_size
                if declared_total > max_total_size:
                    raise UploadValidationError("DOCX ZIP 解压总量超过上限")
                if info.file_size:
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > max_compression_ratio:
                        raise UploadValidationError("DOCX ZIP 单个成员压缩比超过上限")

            missing = _DOCX_REQUIRED_MEMBERS.difference(info.filename for info in infos)
            if missing:
                names = "、".join(sorted(missing))
                raise UploadValidationError(f"DOCX 缺少必需成员: {names}")

            actual_total = 0
            for info in infos:
                actual_member = 0
                with archive.open(info, "r") as member:
                    while True:
                        chunk = member.read(_VALIDATION_CHUNK_SIZE)
                        if not chunk:
                            break
                        actual_member += len(chunk)
                        actual_total += len(chunk)
                        if actual_member > max_member_size:
                            raise UploadValidationError("DOCX ZIP 单个成员解压大小超过上限")
                        if actual_total > max_total_size:
                            raise UploadValidationError("DOCX ZIP 解压总量超过上限")
    except UploadValidationError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise UploadValidationError("DOCX 不是有效的 ZIP 文档") from exc
