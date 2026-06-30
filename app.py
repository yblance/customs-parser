import streamlit as st
import fitz  # PyMuPDF
import pandas as pd
import re
import zipfile
import io

def extract_text_from_pdf_bytes(pdf_bytes):
    """直接从内存中的字节流读取 PDF 文本"""
    text = ""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            text += page.get_text("text")
    return text

def extract_fields(text, filename=""):
    """解析报关单文本，提取多产品信息"""
    public_info = {
        "来源文件名": filename,
        "海关编号": "",
        "出口日期": "",
        "合同协议号": "",
        "指运港": ""
    }
    
    # 提取表头
    c_no = re.search(r"海关编号[:：]?\s*([0-9]{12,20})", text)
    if c_no: public_info["海关编号"] = c_no.group(1)
    
    dates = re.findall(r"\b(20\d{6})\b", text)
    if dates: public_info["出口日期"] = dates[0]
    
    contract = re.search(r"合同协议号\s*\n?\s*([A-Za-z0-9\-_]+)", text)
    if contract: 
        public_info["合同协议号"] = contract.group(1)
    else:
        bs = re.search(r"\b(BS-[A-Za-z0-9\-]+)\b", text)
        if bs: public_info["合同协议号"] = bs.group(1)
        
    # 🌟 恢复你测试通过的最稳妥的指运港提取逻辑
    port = re.search(r"指运港\(地区\)[^\n]*\n\s*([^\n]+)", text)
    if port: 
        public_info["指运港"] = port.group(1).strip()
    else:
        # 增加一个备用兜底逻辑，直接抓取附近的中文
        port_alt = re.search(r"指运港\(地区\).*?([\u4e00-\u9fa5]+)", text)
        if port_alt:
            public_info["指运港"] = port_alt.group(1).strip()

    # 切块提取商品明细
    hs_matches = list(re.finditer(r"\b(\d{10})\b", text))
    if not hs_matches:
        return [public_info]
        
    items_data = []
    for i, match in enumerate(hs_matches):
        start_idx = match.start()
        end_idx = hs_matches[i+1].start() if i + 1 < len(hs_matches) else len(text)
        chunk = text[start_idx:end_idx]
        
        item_row = public_info.copy()
        
        # 提取数量
        qtys_found = []
        qty_str = ""
        q_matches = re.findall(r"(\d+(?:\.\d+)?)\s*(千克|个|件|套|双|吨|升|台|辆|克|米|平方米)", chunk)
        if q_matches:
            qty_str = f"{q_matches[0][0]} {q_matches[0][1]}"
            qtys_found = [float(q[0]) for q in q_matches]
        item_row["数量"] = qty_str
        
        # 提取单价与总价
        words = chunk.split()
        nums = []
        for w in words:
            if re.match(r"^\d+(?:\.\d+)?$", w):
                if len(w) == 10: continue 
                if len(w) <= 2 and '.' not in w and int(w) < 50: continue 
                nums.append(float(w))
                
        unit_price, total_price = "", ""
        if len(nums) >= 2:
            found = False
            for q in qtys_found:
                if q > 0 and not found:
                    for idx1 in range(len(nums)):
                        for idx2 in range(idx1+1, len(nums)):
                            p1, p2 = nums[idx1], nums[idx2]
                            if abs(p1 * q - p2) < 2:
                                unit_price, total_price = p1, p2
                                found = True; break
                            elif abs(p2 * q - p1) < 2:
                                unit_price, total_price = p2, p1
                                found = True; break
                        if found: break
            if not found:
                unit_price, total_price = min(nums[0], nums[1]), max(nums[0], nums[1])
        elif len(nums) == 1:
            unit_price = nums[0]
            total_price = unit_price * qtys_found[0] if qtys_found else ""
            
        item_row["单价"] = unit_price
        item_row["总价"] = total_price
        
        # 提取币制
        curr = re.search(r"\b(CNY|USD|EUR|人民币|美元)\b", chunk)
        item_row["币制"] = curr.group(1) if curr else ""
        
        # 减法提取产品名称
        lines = chunk.split('\n')
        name_parts = []
        country = public_info.get("指运港", "俄罗斯")
        remove_patterns = [
            r"商品名称[、,]?规格型号", r"申报数量/申报单位", r"法定数量/法定单位", r"第二数量/第二单位",
            r"目的国\(地区\)", r"指运港\(地区\)", r"单价", r"总价", r"币制", r"数量及单位",
            country, r"中国", r"\b(CNY|USD|EUR|人民币|美元)\b",
            r"\b\d+(?:\.\d+)?\s*(千克|个|件|套|双|吨|平方米|升|台|辆|克|千米|米)\b"
        ]
        
        for line in lines:
            clean_line = line.strip()
            if not clean_line or re.match(r"^\d+(?:\.\d+)?$", clean_line): continue
            
            for p in remove_patterns:
                clean_line = re.sub(p, "", clean_line).strip()
                
            if str(unit_price) == clean_line or str(total_price) == clean_line: continue
                
            if clean_line and clean_line not in ["|", "/", "-", ",", "，"]:
                clean_line = re.sub(r"^[^\w\u4e00-\u9fa5]+", "", clean_line) 
                if clean_line:
                    name_parts.append(clean_line)
                    
        item_row["产品名称"] = " | ".join(name_parts)
        items_data.append(item_row)
        
    return items_data

# ==========================================
# Streamlit 前端交互与流程控制
# ==========================================

st.set_page_config(page_title="海关报关单提取工具", page_icon="📑", layout="wide")

st.title("📑 报关单智能解析与导出工具")
st.markdown("""
通过上传包含 PDF 报关单的 **ZIP 压缩包**，系统将自动读取所有 PDF 文件，
提取多产品明细并自动处理跨行规格，最终生成合并的 Excel 表格。
""")

uploaded_zip = st.file_uploader("请上传包含报关单PDF的ZIP压缩包", type=["zip"])

if uploaded_zip is not None:
    if st.button("🚀 开始解析", type="primary"):
        with st.spinner("正在逐个解析 PDF 文件，请稍候..."):
            data_rows = []
            try:
                with zipfile.ZipFile(uploaded_zip) as z:
                    valid_files = []
                    for info in z.infolist():
                        if info.flag_bits & 0x800:
                            # 规范的自带 UTF-8 标记的文件
                            decoded_name = info.filename
                        else:
                            # 🌟 终极修复：处理没有 UTF-8 标记被强行按 cp437 读取的情况
                            raw_bytes = info.filename.encode('cp437')
                            try:
                                decoded_name = raw_bytes.decode('utf-8')
                            except UnicodeDecode