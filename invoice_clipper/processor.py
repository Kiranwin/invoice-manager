"""
发票处理核心 - 重构版 v2.0
移除风控/验真/邮件，接入百度OCR + LLM Vision 两级识别引擎
"""
import logging
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from .database import (
    init_db, insert_invoice, is_duplicate, exists_by_invoice_number,
)
from .file_utils import (
    ofd_to_pdf, build_archive_path, archive_invoice,
    extract_text_from_pdf  # 仅用于内容预检
)
from .engines import BaiduOCREngine, LLMVisionEngine, TextOCREngine, EngineResult

logger = logging.getLogger(__name__)


class InvoiceProcessor:
    """发票处理器，负责单张发票的完整流程"""

    def __init__(self, config: dict):
        self.cfg = config
        self.db_path = Path(config["storage"]["db_path"]).expanduser()
        self.archive_dir = Path(config["storage"]["base_dir"]).expanduser()

        # 确保目录存在
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        # 初始化数据库（传完整配置，支持 SQLite / PostgreSQL）
        init_db(self.cfg)

        # 初始化识别引擎（按优先级排序）
        self.engines = []
        self._init_engines()

        # 临时变量，用于记录处理中的信息
        self._original_filename = ""
        self._raw_text = ""
        self._error: Optional[str] = None

    def _init_engines(self):
        """初始化识别引擎（按优先级）"""
        engines = []

        # 第1级：本地 TextOCR（如果启用）
        text_ocr = TextOCREngine(self.cfg)
        if text_ocr.is_available():
            engines.append(text_ocr)
            logger.info(f"✅ 注册第1级引擎: {text_ocr.name}")
        else:
            logger.info("⏭️ TextOCR 引擎未启用或依赖缺失")

        # 第2级：百度OCR
        baidu = BaiduOCREngine(self.cfg)
        if baidu.is_available():
            engines.append(baidu)
            logger.info(f"✅ 注册第2级引擎: {baidu.name}")
        else:
            logger.warning("⚠️ 百度OCR引擎不可用，请检查API配置")

        # 第3级：LLM Vision
        llm = LLMVisionEngine(self.cfg)
        if llm.is_available():
            engines.append(llm)
            logger.info(f"✅ 注册第3级引擎: {llm.name}")
        else:
            logger.warning("⚠️ LLM Vision引擎不可用，请检查API配置")

        # 按 priority 排序
        engines.sort(key=lambda e: e.priority)
        self.engines = engines
    def process_file(self, file_path: Path, source: str = "manual") -> Optional[Dict[str, Any]]:
        """
        处理单个文件（PDF/OFD/图片）
        返回入库记录字典，失败返回 None
        """
        file_path = Path(file_path)

        suffix = file_path.suffix.lower()
        if suffix not in [".pdf", ".ofd", ".png", ".jpg", ".jpeg", ".bmp", ".tiff"]:
            logger.warning(f"不支持的文件类型: {suffix}")
            self._error = f"不支持的文件类型: {suffix}"
            return None

        self._original_filename = file_path.name

        # ── 1. 预处理：OFD 转 PDF ───────────────────────────
        working_path = file_path
        if suffix == ".ofd":
            try:
                working_path = ofd_to_pdf(file_path)
                logger.info(f"OFD 转 PDF 成功: {working_path.name}")
            except Exception as e:
                logger.error(f"OFD 转换失败: {e}")
                self._error = f"OFD 转换失败: {e}"
                return None

        # ── 2. 内容预检（可选，快速过滤非发票文件）──────────
        if working_path.suffix.lower() == ".pdf":
            try:
                text = extract_text_from_pdf(working_path)
                self._raw_text = text[:2000] if text else ""
                # 简单检查是否包含发票关键词
                text_lower = text.lower()
                if "发票" not in text_lower and "invoice" not in text_lower:
                    logger.warning(f"文件可能不是发票，跳过: {file_path.name}")
                    # 不直接返回None，继续走识别流程（图片发票可能没有文字层）
            except Exception as e:
                logger.warning(f"PDF 文本预检失败: {e}")
                self._raw_text = ""

        # ── 3. 两级识别 ────────────────────────────────────
        fields = self._recognize(working_path)
        if not fields:
            err = self._error or "所有识别引擎均失败"
            logger.error(f"所有识别引擎均失败: {file_path.name}")
            return None

        # ── 4. 去重检查 ────────────────────────────────────
        inv_no = fields.get("invoice_number", "")
        amount = fields.get("amount_with_tax", 0)
        if inv_no:
            if is_duplicate(inv_no, amount):
                msg = f"重复发票（发票号+金额相同）: {inv_no}"
                logger.warning(msg)
                self._error = msg
                return None
            if exists_by_invoice_number(inv_no):
                msg = f"重复发票（发票号已存在）: {inv_no}"
                logger.warning(msg)
                self._error = msg
                return None

        # ── 5. 归档文件 ────────────────────────────────────
        dest_path = build_archive_path(self.archive_dir, fields)
        archived = archive_invoice(working_path, dest_path, move=True)

        # OFD 原文件也归档（可选）
        if suffix == ".ofd" and file_path.exists():
            ofd_archive_dir = self.archive_dir / "ofd_original"
            ofd_archive_dir.mkdir(parents=True, exist_ok=True)
            ofd_archive_path = ofd_archive_dir / file_path.name
            archive_invoice(file_path, ofd_archive_path, move=True)

        # ── 6. 构建记录并入库 ──────────────────────────────
        record = self._build_record(fields, archived, source)
        try:
            invoice_id = insert_invoice(record)
        except Exception:
            archived.unlink(missing_ok=True)
            raise
        record["id"] = invoice_id

        logger.info(
            f"✅ 入库 #{invoice_id}: {fields.get('invoice_date')} | "
            f"{fields.get('seller_name')} | ¥{amount:.2f} | {inv_no}"
        )
        return record

    def _recognize(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """执行三级识别引擎"""
        errors = []
        for engine in self.engines:
            logger.info(f"尝试第{engine.priority}级引擎: {engine.name}")
            result = engine.extract(str(file_path))

            if result.is_valid:
                logger.info(f"✅ {engine.name} 识别成功，置信度={result.confidence:.2f}")
                # 保存原始JSON供调试
                if hasattr(result, 'raw_json'):
                    self._raw_json = result.raw_json
                self._error = None
                return result.data

            err_msg = result.error or '无效结果'
            logger.warning(f"⚠️ {engine.name} 失败: {err_msg}")
            errors.append(f"{engine.name}: {err_msg}")

        self._error = "所有识别引擎均失败: " + "; ".join(errors)
        return None

    def _build_record(self, fields: Dict[str, Any], archived_path: Path, source: str) -> Dict[str, Any]:
        """构建入库记录字典"""
        # 处理可能缺失的字段，确保类型正确
        def _str(val, default=""):
            return str(val) if val else default

        def _float(val, default=0.0):
            if val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        return {
            "invoice_number": _str(fields.get("invoice_number")),
            "invoice_code": _str(fields.get("invoice_code")),
            "invoice_date": _str(fields.get("invoice_date")),
            "commodity_name": _str(fields.get("commodity_name")),
            "specification_model": _str(fields.get("specification_model")),
            "buyer_name": _str(fields.get("buyer_name")),
            "buyer_tax_num": _str(fields.get("buyer_tax_num")),
            "seller_name": _str(fields.get("seller_name")),
            "seller_tax_num": _str(fields.get("seller_tax_num")),
            "tax_rate": _float(fields.get("tax_rate")),
            "tax_amount": _float(fields.get("tax_amount")),
            "amount_with_tax": _float(fields.get("amount_with_tax")),
            "category": _str(fields.get("category"), "其他"),
            "belong_project": "",      # 待用户编辑
            "belong_person": "",
            "remark": "",
            "source": source,
            "original_filename": self._original_filename,
            "stored_path": str(archived_path),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "raw_text": getattr(self, "_raw_text", "")[:2000],
            "raw_json": json.dumps(fields, ensure_ascii=False),
        }

    def process_directory(self, dir_path: Path, source: str = "dir") -> list:
        """批量处理目录中的发票文件"""
        dir_path = Path(dir_path)
        results = []
        patterns = ["*.pdf", "*.PDF", "*.ofd", "*.OFD", "*.png", "*.PNG", "*.jpg", "*.JPG", "*.jpeg", "*.JPEG"]
        files = []
        for pat in patterns:
            files.extend(dir_path.glob(pat))

        total = len(files)
        if total == 0:
            logger.info(f"目录无发票文件: {dir_path}")
            return results

        logger.info(f"发现 {total} 个文件待处理: {dir_path}")

        # 尝试使用 tqdm 进度条
        try:
            from tqdm import tqdm
            iterator = tqdm(files, desc="处理进度", unit="张")
        except ImportError:
            iterator = files

        for f in iterator:
            result = self.process_file(f, source=source)
            if result:
                results.append(result)

        return results
