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

def extract_port(text):
    """指运港：兼容全/半角括号 + 跨行 + 跨段"""
    boundary = r"(?=境内货源地|批准文号|成交方式|合同协议号|件数)"
    m = re.search(r"指运港[^\r\n]*\r?\n([\s\S]*?)" + boundary, text)
    if not m:
        return ""
    raw = m.group(1)
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    # 过滤掉单独的闭合括号 "）" ")"
    lines = [l for l in lines if l not in {"）", ")"}]
    if not lines:
        return ""
    # 主名行：排除以 ( 或 （ 开头的残段
    main_lines = [l for l in lines if not (l.startswith("（") or l.startswith("("))]
    port = " ".join(main_lines) if main_lines else lines[0]
    # 去掉尾部的国家/地区代码括号，如 "(344)" 或 "（俄罗斯）"
    port = re.sub(r"[\(（][^()（）]*[\)）]\s*$", "", port).strip()
    # 兜底：若仍有未闭合的 "(xxx" 残段，从最后一个 ( 截断
    port = re.sub(r"[\(（][^()（）]*$", "", port).strip()
    return port

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
        
    # 🌟 修复指运港 Bug：兼容全/半角括号、跨行、多行抓取
    port = extract_port(text)
    if port:
        public_info["指运港"] = port

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
        country = public_info.get("指运港", "")  # 若指运港未拿到，留空避免硬编码误删
        remove_patterns = [
            r"商品名称[、,]?规格型号", r"申报数量/申报单位", r"法定数量/法定单位", r"第二数量/第二单位",
            r"目的国[\(（][^()（）]*[\)）]", r"指运港[\(（][^()（）]*[\)）]", r"单价", r"总价", r"币制", r"数量及单位",
        ]
        if country:
            remove_patterns.append(rf"^{re.escape(country)}$")
        remove_patterns += [
            r"中国", r"\b(CNY|USD|EUR|人民币|美元)\b",
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
                    # 🌟 修复乱码 Bug：分情况解码 ZIP 中的文件名
                    # 1. 若声明了 UTF-8 标志位（位 0x800），zipfile 已按 UTF-8 解码，直接用
                    # 2. 否则用 filename_raw 拿原始字节，依次尝试 GBK / GB2312 / Big5，都不行再退回 UTF-8
                    valid_files = []
                    for info in z.infolist():
                        if info.flag_bits & 0x800:
                            decoded_name = info.filename
                        else:
                            raw_bytes = getattr(info, "filename_raw", None) or info.filename.encode("utf-8", errors="replace")
                            decoded_name = None
                            for enc in ("gbk", "gb2312", "big5"):
                                try:
                                    decoded_name = raw_bytes.decode(enc)
                                    break
                                except UnicodeDecodeError:
                                    continue
                            if decoded_name is None:
                                decoded_name = raw_bytes.decode("utf-8", errors="replace")

                        if decoded_name.lower().endswith('.pdf') and not decoded_name.startswith('__MACOSX'):
                            valid_files.append((info, decoded_name))
                    
                    if not valid_files:
                        st.error("❌ 压缩包中没有找到有效的 PDF 文件，请检查。")
                        st.stop()

                    progress_bar = st.progress(0)
                    for idx, (info, filename) in enumerate(valid_files):
                        display_name = filename.split('/')[-1] if '/' in filename else filename
                        
                        # 使用原始的 info 对象读取文件内容，但传递解码后的名称
                        with z.open(info) as f:
                            pdf_bytes = f.read()
                            text = extract_text_from_pdf_bytes(pdf_bytes)
                            rows = extract_fields(text, filename=display_name)
                            data_rows.extend(rows)
                            
                        progress_bar.progress((idx + 1) / len(valid_files))

                if data_rows:
                    df = pd.DataFrame(data_rows)
                    cols = ["来源文件名", "海关编号", "出口日期", "合同协议号", "指运港", "产品名称", "数量", "单价", "总价", "币制"]
                    df = df.reindex(columns=cols)
                    
                    st.success(f"✅ 解析完成！共提取到 {len(data_rows)} 条产品记录。")
                    st.dataframe(df, use_container_width=True)
                    
                    excel_buffer = io.BytesIO()
                    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                        df.to_excel(writer, index=False, sheet_name='报关数据明细')
                    excel_data = excel_buffer.getvalue()
                    
                    st.download_button(
                        label="📥 下载 Excel 报表",
                        data=excel_data,
                        file_name="报关单提取汇总.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                else:
                    st.warning("⚠️ 没有提取到任何有效数据，请确保上传的 PDF 是标准报关单格式。")

            except Exception as e:
                st.error(f"解析过程中出现错误: {e}")