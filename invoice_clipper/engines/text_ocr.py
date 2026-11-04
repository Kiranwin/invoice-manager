"""
第1级引擎：TextOCR（本地 OCR 混合方案）
使用 PyMuPDF 提取可搜索文本，若不足则调用 PaddleOCR 识别扫描件
"""
import re
import logging
from pathlib import Path
from typing import Dict, Any

from .base import BaseEngine, EngineResult
from ._utils import normalize_date, infer_category, calculate_confidence

logger = logging.getLogger(__name__)

# 全局 PaddleOCR 实例（懒加载）
_ocr_instance = None


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        try:
            from paddleocr import PaddleOCR
            logger.info("正在初始化 PaddleOCR 模型（首次使用可能较慢）...")
            _ocr_instance = PaddleOCR(use_angle_cls=True, lang='ch')
        except ImportError:
            raise RuntimeError("PaddleOCR 未安装，请运行: pip install paddlepaddle paddleocr")
    return _ocr_instance


class TextOCREngine(BaseEngine):
    """
    本地 OCR 引擎（PyMuPDF + PaddleOCR 回退）
    无需 API 密钥，完全本地运行
    """

    name = "text_ocr"
    priority = 1  # 最高优先级（如果启用）

    def __init__(self, config: dict):
        self.cfg = config
        ocr_cfg = config.get("ocr", {}).get("text_ocr", {})
        self.enabled = ocr_cfg.get("enabled", False)
        self.text_threshold = ocr_cfg.get("text_threshold", 100)
        self.ocr_fallback = ocr_cfg.get("ocr_fallback", True)
        self.pdf_dpi = ocr_cfg.get("pdf_dpi", 200)  # 用于 OCR 渲染分辨率

    def is_available(self) -> bool:
        if not self.enabled:
            logger.debug("TextOCR 引擎已禁用")
            return False
        try:
            import fitz  # PyMuPDF
            # PaddleOCR 懒加载，不在此处检查
            return True
        except ImportError as e:
            logger.warning(f"TextOCR 引擎依赖未安装: {e}")
            return False

    def extract(self, file_path: str) -> EngineResult:
        path = Path(file_path)
        if path.suffix.lower() != ".pdf":
            return EngineResult(
                data=None,
                confidence=0,
                engine=self.name,
                error="TextOCR 仅支持 PDF 文件",
            )

        try:
            # 1. 提取全文
            full_text = self._extract_full_text(str(path))

            # 2. 解析字段
            fields = self._parse_invoice_info(full_text)

            # 3. 后处理标准化
            fields = self._post_process(fields)

            # 4. 计算置信度
            confidence = calculate_confidence(fields)

            return EngineResult(
                data=fields,
                confidence=confidence,
                engine=self.name,
                raw_text=full_text[:2000],
            )

        except Exception as e:
            return EngineResult(
                data=None,
                confidence=0,
                engine=self.name,
                error=f"识别异常: {e}",
            )

    def _extract_full_text(self, pdf_path: str) -> str:
        """提取全文：先用 PyMuPDF 提取文本，不足则用 OCR"""
        # 尝试 PyMuPDF 提取文本
        text = self._extract_with_pymupdf(pdf_path)
        if len(text) > self.text_threshold:
            logger.info(f"PyMuPDF 提取成功，字符数: {len(text)}")
            return text

        if self.ocr_fallback:
            logger.info(f"PyMuPDF 提取不足({len(text)}字符)，启用 OCR 识别")
            return self._extract_with_ocr(pdf_path)
        else:
            logger.warning("OCR 回退已禁用，返回短文本")
            return text

    def _extract_with_pymupdf(self, pdf_path: str) -> str:
        """使用 PyMuPDF 提取 PDF 中的可搜索文本"""
        try:
            import fitz
            full_text = ""
            with fitz.open(pdf_path) as doc:
                for page in doc:
                    text = page.get_text("text", sort=True)
                    if text:
                        full_text += text + "\n"
            return full_text.strip()
        except Exception as e:
            logger.error(f"PyMuPDF 文本提取失败: {e}")
            return ""

    def _extract_with_ocr(self, pdf_path: str) -> str:
        """使用 PyMuPDF 渲染页面为图像，再调用 PaddleOCR 识别"""
        try:
            import fitz
            import numpy as np
            from PIL import Image
            ocr_model = _get_ocr()

            doc = fitz.open(pdf_path)
            all_texts = []
            for page_num in range(len(doc)):
                logger.debug(f"OCR 识别第 {page_num+1}/{len(doc)} 页...")
                page = doc[page_num]
                # 根据 DPI 计算缩放矩阵（默认 200 DPI，fitz 默认 72 DPI）
                zoom = self.pdf_dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                # 转换为 PIL Image
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                # 转换为 numpy 数组供 PaddleOCR 使用
                img_np = np.array(img)
                result = ocr_model.ocr(img_np, cls=True)
                if result and result[0] is not None:
                    page_text = ' '.join([line[1][0] for line in result[0]])
                    all_texts.append(page_text)
            doc.close()
            return '\n'.join(all_texts)
        except Exception as e:
            logger.error(f"OCR 识别失败: {e}")
            return ""

    # ---------- 字段解析逻辑（基于用户提供，已适配新字段名）----------
    def _parse_invoice_info(self, text: str) -> Dict[str, Any]:
        flags = re.DOTALL | re.IGNORECASE
        info = {}

        # 发票号码
        patterns_num = [r'发票号码[：:]\s*(\d+)', r'发票号[：:]\s*(\d+)']
        info['invoice_number'] = self._try_patterns(patterns_num, text, flags)

        # 发票代码
        patterns_code = [r'发票代码[：:]\s*(\d+)']
        info['invoice_code'] = self._try_patterns(patterns_code, text, flags)

        # 开票日期
        patterns_date = [
            r'开票日期[：:]\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)',
            r'开票日期[：:]\s*(\d{4}-\d{1,2}-\d{1,2})',
        ]
        date_str = self._try_patterns(patterns_date, text, flags)
        info['invoice_date'] = normalize_date(date_str) if date_str else ''

        # 购买方名称
        patterns_buyer = [
            r'购\s*名称[：:]\s*(.*?)(?=\s+销\s*名称|$)',
            r'购买方名称[：:]\s*(.*?)(?=\s+销售方名称|$)',
            r'买方[：:]\s*(.*?)(?=\s+卖方|$)',
        ]
        info['buyer_name'] = self._try_patterns(patterns_buyer, text, flags)

        # 购买方税号
        patterns_buyer_tax = [
            r'购买方统一社会信用代码/纳税人识别号[：:]\s*([A-Z0-9\s]{15,25})',
            r'统一社会信用代码/纳税人识别号[：:]\s*([A-Z0-9\s]{15,25})',
            r'纳税人识别号[：:]\s*([A-Z0-9\s]{15,25})',
        ]
        tax = self._try_patterns(patterns_buyer_tax, text, flags)
        info['buyer_tax_num'] = re.sub(r'\s+', '', tax) if tax else ''

        # 销售方名称
        patterns_seller = [
            r'销\s*名称[：:]\s*([^\n]{2,40})',
            r'售\s*名称[：:]\s*([^\n]{2,40})',
            r'销售方名称[：:]\s*([^\n]{2,40})',
            r'卖方[：:]\s*([^\n]{2,40})',
        ]
        info['seller_name'] = self._try_patterns(patterns_seller, text, flags)

        # 销售方税号
        patterns_seller_tax = [
            r'销售方统一社会信用代码/纳税人识别号[：:]\s*([A-Z0-9\s]{15,25})',
            r'销货单位纳税人识别号[：:]\s*([A-Z0-9\s]{15,25})',
        ]
        tax = self._try_patterns(patterns_seller_tax, text, flags)
        info['seller_tax_num'] = re.sub(r'\s+', '', tax) if tax else ''

        # 表格区域提取商品/税率/税额
        table_section = self._extract_table_section(text)

        # 商品名称
        patterns_commodity = [
            r'\*[^*]*\*[^\s]+',
            r'\*[^*]*\*.*?(?=\s+\d)',
            r'^[\s]*([\u4e00-\u9fa5a-zA-Z0-9·\-\(\)]+)',
        ]
        info['commodity_name'] = self._extract_from_table(table_section, patterns_commodity, group=0)

        # 规格型号
        patterns_spec = [
            r'\*[^*]*\*\s*([A-Za-z0-9\s]+?)(?=\s+\d+\s+[\d\.]+)',
            r'[\u4e00-\u9fa5a-zA-Z0-9·\-]+?\s+([A-Za-z0-9\-]+)(?=\s+\d+\s+[\d\.]+)',
        ]
        info['specification_model'] = self._extract_from_table(table_section, patterns_spec, group=1)

        # 税率
        patterns_taxrate = [
            r'(\d+\.?\d*%)(?=\s*\d*\.?\d*\s*$)',
            r'税率/征收率.*?(\d+\.?\d*%)',
        ]
        rate_str = self._extract_from_table(table_section, patterns_taxrate)
        if rate_str and rate_str != '未识别':
            rate_str = rate_str.replace('%', '')
            try:
                info['tax_rate'] = float(rate_str) / 100.0
            except ValueError:
                info['tax_rate'] = None
        else:
            info['tax_rate'] = None

        # 税额
        patterns_taxamt = [
            r'(\d+\.\d{2})(?=\s*$)',
            r'税额\s+(\d+\.\d{2})',
        ]
        tax_amt = self._extract_from_table(table_section, patterns_taxamt)
        info['tax_amount'] = float(tax_amt) if tax_amt and tax_amt != '未识别' else 0.0

        # 价税合计
        patterns_amount = [
            r'价税合计[（(]?大写[）)]?.*?（小写）[¥￥]?\s*([\d,]+\.?\d*)',
            r'（小写）[¥￥]\s*([\d,]+\.?\d*)',
            r'价税合计[：:]\s*[¥￥]?\s*([\d,]+\.?\d*)',
            r'合\s*计\s*[¥￥]\s*([\d,]+\.?\d*)',
        ]
        val = self._try_patterns(patterns_amount, text, flags)
        if val and val != '未识别':
            info['amount_with_tax'] = float(val.replace(',', ''))
        else:
            info['amount_with_tax'] = 0.0

        # 分类推断
        info['category'] = infer_category(info.get('seller_name', ''))

        return info

    def _try_patterns(self, patterns, text, flags):
        for p in patterns:
            m = re.search(p, text, flags)
            if m:
                return m.group(1).strip()
        return ''

    def _extract_table_section(self, text):
        m = re.search(r'项目名称.*?合\s*计', text, re.DOTALL)
        return m.group(0) if m else text

    def _extract_from_table(self, table_text, patterns, group=1):
        if not table_text:
            return '未识别'
        lines = table_text.split('\n')
        header_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额']
        for line in lines:
            line = line.strip()
            if not line or any(kw in line for kw in header_keywords):
                continue
            for p in patterns:
                m = re.search(p, line, re.IGNORECASE)
                if m:
                    if group == 0:
                        return m.group(0).strip()
                    else:
                        return m.group(group).strip()
        return '未识别'

    def _post_process(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        # 确保金额类型正确
        for k in ['tax_amount', 'amount_with_tax']:
            if fields.get(k) == '未识别':
                fields[k] = 0.0
        if fields.get('tax_rate') == '未识别':
            fields['tax_rate'] = None
        return fields

