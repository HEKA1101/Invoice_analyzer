import streamlit as st
import pandas as pd
import pdfplumber
import re
import io

# ---------- 工具函数：安全转 float ----------
def safe_float(x):
    """
    尝试把任意输入安全地转换成 float。
    - 去掉前后空格
    - 去掉千分位逗号
    - 不能转换则返回 None
    """
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None

# ---------- 税额解析（只用“确定的数据”） ----------
def parse_tax_amount(token, tax_rate):
    """
    解析税额:
    - 如果 token 是明确数字 => 直接用这个数字
    - 如果 token 是占位符(***, -- 等) 或空:
        - 如果税率字段明确是 0 税率(免税、不征、零税率、0%) => 记为 0.0
        - 否则 => 返回 None（未知，不做估算）
    - 其他情况 => 返回 None
    """
    token_str = "" if token is None else str(token).strip()
    rate_str  = "" if tax_rate is None else str(tax_rate).strip()

    # 1) 优先看税额格子里是不是明确的数字
    val = safe_float(token_str)
    if val is not None:
        return val

    # 2) 下面这些视为“占位符 / 空”，本身不表示数值
    missing_like = {"", "***", "＊＊＊", "--", "-", "—", "―", "*"}
    if token_str in missing_like:
        zero_rate_keywords = ("免税", "不征", "零税率", "0%")
        if any(kw in rate_str for kw in zero_rate_keywords):
            return 0.0
        else:
            return None

    # 3) 其他奇怪字符串，一律视为未知
    return None

# ---------- 工具函数：把 *蔬菜*芥兰苗 拆成 类别 + 商品 ----------
def split_category_item(name: str):
    if not isinstance(name, str):
        name = str(name) if name is not None else ""
    name = name.strip()
    m = re.match(r"\*(?P<cat>[^*]+)\*(?P<item>.+)", name)
    if m:
        return m.group("cat").strip(), m.group("item").strip()
    return "未分类", name

# ---------- 从第一页文字里提取发票抬头信息 ----------
def parse_header_text(text: str):
    info = {}
    if not text:
        return info

    # 发票号码
    m = re.search(r"发票号码[:：]\s*(\d+)", text)
    if m:
        info["发票号码"] = m.group(1)

    # 开票日期
    m = re.search(r"开票日期[:：]\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", text)
    if m:
        info["开票日期"] = m.group(1)

    # 名称（买方 / 卖方）
    names = re.findall(r"名称[:：]\s*([^\s]+)", text)
    if len(names) >= 1:
        info["购买方名称"] = names[0]
    if len(names) >= 2:
        info["销售方名称"] = names[1]

    # 统一社会信用代码/纳税人识别号
    codes = re.findall(r"统一社会信用代码/纳税人识别号[:：]\s*([0-9A-Z]+)", text)
    if len(codes) >= 1:
        info["购买方税号"] = codes[0]
    if len(codes) >= 2:
        info["销售方税号"] = codes[1]

    return info

# ---------- 核心：按“每一行文本”解析发票 ----------
def parse_invoice_pdf(uploaded_file):
    """
    按行解析一张 PDF 发票。
    每一行一般类似：
    *蔬菜*芥兰苗 斤 72 5 360.00 免税 ***
    或：
    *蔬菜*芥兰苗  斤  72  5  360.00  9%  32.40

    我们从“行尾”开始抓：
    - 倒数第 1 个 token -> 税额
    - 倒数第 2 个 token -> 税率
    - 倒数第 3 个 token -> 金额（不含税）
    剩下中间的部分再去解析 单位 / 数量 / 单价。
    """
    file_bytes = uploaded_file.read()
    rows = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        # 先从第一页拿抬头信息
        header_text = pdf.pages[0].extract_text() or ""
        header_info = parse_header_text(header_text)

        # 再逐页按行解析明细
        for page_idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue

                # 只解析包含 "*类别*商品" 的行
                m = re.match(r"\*(?P<cat>[^*]+)\*(?P<item>.+)", line)
                if not m:
                    continue

                tokens = line.split()
                # 至少要有：名称 + 金额 + 税率 + 税额 共 4 个 token
                if len(tokens) < 4:
                    continue

                name_token = tokens[0]

                # ----- 从行尾往前：金额 / 税率 / 税额 -----
                tax_amount_token = tokens[-1]
                tax_rate_token   = tokens[-2]
                amount_token     = tokens[-3]

                middle_tokens = tokens[1:-3]  # 名称之后、金额之前的部分

                amount = safe_float(amount_token)
                tax_rate = tax_rate_token
                tax_amount = parse_tax_amount(tax_amount_token, tax_rate)

                # ----- 解析 unit / qty / price（优先从数字往回找） -----
                unit = None
                qty = None
                price = None

                if middle_tokens:
                    num_indices = [i for i, t in enumerate(middle_tokens) if safe_float(t) is not None]

                    if len(num_indices) >= 2:
                        idx_price = num_indices[-1]
                        idx_qty   = num_indices[-2]
                        price = safe_float(middle_tokens[idx_price])
                        qty   = safe_float(middle_tokens[idx_qty])

                        unit_idx = idx_qty - 1
                        if unit_idx >= 0:
                            unit = middle_tokens[unit_idx]

                    elif len(num_indices) == 1:
                        idx_qty = num_indices[0]
                        qty = safe_float(middle_tokens[idx_qty])
                        unit_idx = idx_qty - 1
                        if unit_idx >= 0:
                            unit = middle_tokens[unit_idx]

                    # 如果还是没拿到单位，但中间有字符串，就用最前面的一个当单位
                    if unit is None and middle_tokens:
                        unit = middle_tokens[0]

                # ----- 计算含税价（价税合计） -----
                base_amount = amount
                if base_amount is not None and tax_amount is not None:
                    gross = base_amount + tax_amount
                elif base_amount is not None:
                    gross = base_amount
                else:
                    gross = None

                category, item = split_category_item(name_token)

                data = {
                    "发票文件": uploaded_file.name,
                    "页码": page_idx + 1,
                    "类别": category,
                    "商品": item,
                    "单位": unit,
                    "数量": qty,
                    "单价": price,
                    "金额": base_amount,   # 不含税金额
                    "税率": tax_rate,
                    "税额": tax_amount,
                    "含税价": gross,       # 金额 + 税额
                    "原始项目名称": name_token,
                }
                data.update(header_info)
                rows.append(data)

    if not rows:
        cols = [
            "发票文件", "发票号码", "开票日期",
            "购买方名称", "购买方税号",
            "销售方名称", "销售方税号",
            "页码", "类别", "商品", "单位",
            "数量", "单价", "金额", "税率", "税额", "含税价",
            "原始项目名称",
        ]
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(rows)

    # 数值列统一转成 float
    for col in ["数量", "单价", "金额", "税额", "含税价"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df

# ---------- 关键词搜索 ----------
def search_items(df: pd.DataFrame, query: str) -> pd.DataFrame:
    if not query:
        return df
    q = query.strip()
    mask = df["类别"].astype(str).str.contains(q, na=False) | \
           df["商品"].astype(str).str.contains(q, na=False)
    return df[mask]

# ---------- 日期简化：YYYY年MM月DD日 -> YYYY-MM-DD ----------
def format_date_short(raw):
    if not raw:
        return ""
    s = str(raw).strip()
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return s

# ==================== Streamlit 页面 ====================

st.subheader("电子发票自动分类汇总")

st.markdown(
    """
1. 上传一张或多张电子发票 PDF  
2. 程序自动识别发票内容  
3. 提取 `*类别*商品` 信息，并按类别汇总金额  
4. 可以输入关键词筛选明细
"""
)

uploaded_files = st.file_uploader(
    "上传电子发票 （PDF）",
    type=["pdf"],
    accept_multiple_files=True,
)

# ====== 解析按钮 + 状态初始化 ======

if "df_all" not in st.session_state:
    st.session_state["df_all"] = None
    st.session_state["n_files"] = 0

if st.button("开始解析并汇总"):
    if not uploaded_files:
        st.warning("请先上传至少一份发票 PDF。")
        st.session_state["df_all"] = None
        st.session_state["n_files"] = 0
    else:
        all_dfs = []
        seen_invoices = set()

        for f in uploaded_files:
            df_one = parse_invoice_pdf(f)
            if df_one.empty:
                st.warning(f"文件 {f.name} 没有解析到明细行，请确认格式。")
                continue

            invoice_no = None
            issue_date = None
            buyer_name = None
            seller_name = None

            if "发票号码" in df_one.columns and df_one["发票号码"].notna().any():
                invoice_no = df_one["发票号码"].dropna().iloc[0]
            if "开票日期" in df_one.columns and df_one["开票日期"].notna().any():
                issue_date = df_one["开票日期"].dropna().iloc[0]
            if "购买方名称" in df_one.columns and df_one["购买方名称"].notna().any():
                buyer_name = df_one["购买方名称"].dropna().iloc[0]
            if "销售方名称" in df_one.columns and df_one["销售方名称"].notna().any():
                seller_name = df_one["销售方名称"].dropna().iloc[0]

            if any([invoice_no, issue_date, buyer_name, seller_name]):
                key = (invoice_no, issue_date, buyer_name, seller_name)
            else:
                key = ("FILE_ONLY", f.name)

            if key in seen_invoices:
                if invoice_no or issue_date:
                    msg = (
                        "检测到重复发票：  \n"
                        f"发票号：{invoice_no or '未知'}  \n"
                        f"日期：{issue_date or '未知'}  \n"
                        f"文件：{f.name} 已被自动忽略。"
                    )
                    st.warning(msg)
                else:
                    st.warning(f"文件 {f.name} 与之前上传的文件重复（根据文件名判断），已自动忽略。")
                continue

            seen_invoices.add(key)
            all_dfs.append(df_one)

        if not all_dfs:
            st.error("所有文件都没有解析到有效的发票明细（或全部为重复发票），请检查格式。")
            st.session_state["df_all"] = None
            st.session_state["n_files"] = 0
        else:
            df_all = pd.concat(all_dfs, ignore_index=True)
            st.session_state["df_all"] = df_all
            st.session_state["n_files"] = len(all_dfs)
            st.success("解析完成，可以在下方查看汇总结果。")

# ====== 展示部分 ======
st.markdown("---")
df_all = st.session_state.get("df_all")

if df_all is not None and not df_all.empty:
    n_files = st.session_state.get("n_files", 1)

    # 所有金额相关汇总一律使用“含税价”这一列（没有就退回“金额”）
    amount_col = "含税价" if "含税价" in df_all.columns else "金额"
    total_amount = df_all[amount_col].sum()

    # ========== 情况 1：只上传一个文件 ==========
    if n_files <= 1:
        summary = (
            df_all.groupby("类别", dropna=False)[amount_col]
                  .sum()
                  .reset_index()
                  .sort_values(amount_col, ascending=False)
                  .reset_index(drop=True)
        )

        st.subheader("按类别汇总金额（含税）")

        if "开票日期" in df_all.columns and df_all["开票日期"].notna().any():
            first_date_raw = df_all["开票日期"].dropna().iloc[0]
            first_date_short = format_date_short(first_date_raw)
            date_label = first_date_short
            st.write(f"**开票日期：{first_date_short}**")
        else:
            date_label = "本次"
            st.write("**开票日期：未知**")

        st.dataframe(summary, use_container_width=True, hide_index=True)
        st.write(f"**{date_label} 发票价税合计金额：{total_amount:.2f} 元**")

    # ========== 情况 2：上传多个文件 ==========
    else:
        has_date = ("开票日期" in df_all.columns) and df_all["开票日期"].notna().any()
        handled_special = False

        if has_date:
            df_all["开票日期_短"] = df_all["开票日期"].apply(
                lambda x: format_date_short(x) if pd.notna(x) else ""
            )
            dim_col = "开票日期_短"
            dim_label = "开票日期"
            date_values = sorted(
                d for d in df_all[dim_col].dropna().unique() if d
            )

            # 多文件但只有一个日期 → 按单日期汇总（无透视表和合计行列）
            if len(date_values) == 1:
                only_date = date_values[0]

                st.subheader("按类别汇总金额（含税）")
                st.write(f"**开票日期：{only_date}**")

                summary = (
                    df_all.groupby("类别", dropna=False)[amount_col]
                          .sum()
                          .reset_index()
                          .sort_values(amount_col, ascending=False)
                          .reset_index(drop=True)
                )
                st.dataframe(summary, use_container_width=True, hide_index=True)
                st.write(f"**{only_date} 发票价税合计金额：{total_amount:.2f} 元**")
                handled_special = True

        if not has_date:
            dim_col = "发票文件"
            dim_label = "发票文件"
            date_values = sorted(
                d for d in df_all[dim_col].dropna().unique() if d
            )

        # ---------- 一般情况：有多个不同日期/文件 ----------
        if not handled_special:
            st.subheader("按 类别×日期 汇总金额（含税）")

            # 汇总表：行=类别，列=日期/文件，带行列合计
            pivot_sum = pd.pivot_table(
                df_all,
                values=amount_col,
                index="类别",
                columns=dim_col,
                aggfunc="sum",
                fill_value=0,
            )
            if date_values:
                pivot_sum = pivot_sum.reindex(columns=date_values)

            pivot_sum["合计"] = pivot_sum.sum(axis=1)
            total_row = pd.DataFrame(pivot_sum.sum()).T
            total_row.index = ["合计"]
            pivot_sum = pd.concat([pivot_sum, total_row])

            pivot_sum.index.name = "类别"
            pivot_sum_display = pivot_sum.reset_index()
            pivot_sum_display = pivot_sum_display.rename(columns={"index": "类别"})

            # 透视表：行=日期/文件，列=类别，带行列合计
            pivot_view = pd.pivot_table(
                df_all,
                values=amount_col,
                index=dim_col,
                columns="类别",
                aggfunc="sum",
                fill_value=0,
            )
            pivot_view["合计"] = pivot_view.sum(axis=1)
            total_row2 = pd.DataFrame(pivot_view.sum()).T
            total_row2.index = ["合计"]
            pivot_view = pd.concat([pivot_view, total_row2])

            pivot_view.index.name = dim_col
            pivot_view = pivot_view.reset_index().rename(columns={dim_col: dim_label})

            # --- 组合下拉选项 ---
            summary_label = f"汇总表：类别 × 日期"
            pivot_label = f"透视表：日期 × 类别"
            date_label_map = {f"{v}": v for v in date_values}
            mode_options = [summary_label, pivot_label] + list(date_label_map.keys())

            mode = st.selectbox("选择显示方式：", mode_options, index=0)

            # ===== 显示汇总表（类别 × 日期/文件） =====
            if mode == summary_label:
                st.caption("**汇总表（含税）**")
                st.dataframe(
                    pivot_sum_display,
                    use_container_width=True,
                    hide_index=True
                )
                st.write(f"**本次上传发票价税合计金额：{total_amount:.2f} 元**")

            # ===== 显示透视表（日期/文件 × 类别） =====
            elif mode == pivot_label:
                st.caption("**透视表（含税）**")
                st.dataframe(
                    pivot_view,
                    use_container_width=True,
                    hide_index=True
                )
                st.write(f"**本次上传发票价税合计金额：{total_amount:.2f} 元**")

            # ===== 某个具体日期/文件的按类别汇总 =====
            else:
                selected_val = date_label_map.get(mode)
                if selected_val is not None:
                    df_val = df_all[df_all[dim_col] == selected_val]
                    summary_val = (
                        df_val.groupby("类别", dropna=False)[amount_col]
                              .sum()
                              .reset_index()
                              .sort_values(amount_col, ascending=False)
                              .reset_index(drop=True)
                    )
                    st.caption(f"{selected_val} 按类别汇总金额（含税）")
                    st.dataframe(
                        summary_val,
                        use_container_width=True,
                        hide_index=True
                    )
                    st.write(
                        f"**{selected_val} 发票价税合计金额：{df_val[amount_col].sum():.2f} 元**"
                    )
                    st.write(f"**本次上传发票价税合计金额：{total_amount:.2f} 元**")
                else:
                    st.info(f"当前没有可用的 {dim_label} 信息。")

    # ==================================================================
    #         报销伙食费明细（压缩大类，使用者自由选择开票日期）
    # ==================================================================
    st.markdown("---")

    if "类别" in df_all.columns:
        # 1) 先做类别 -> 报销项目 的映射
        big_cat_map = {
            "畜禽产品": "肉蛋禽",
            "肉及肉制品": "肉蛋禽",
            "植物油": "粮油",
            "调味品": "粮油",
            "谷物加工品": "粮油",
            "谷物细粉": "粮油",
            "海水产品": "海鲜",
            "蔬菜": "蔬菜",
            "水果": "蔬菜",
        }

        df_big = df_all.copy()
        df_big["报销项目"] = df_big["类别"].map(big_cat_map)
        df_big = df_big[df_big["报销项目"].notna()]

        if not df_big.empty:
            st.subheader("报销伙食费明细")

            # 2) 准备一个用于筛选的“报销日期”列
            if "开票日期_短" in df_big.columns:
                df_big["报销日期"] = df_big["开票日期_短"].fillna("")
            elif "开票日期" in df_big.columns:
                df_big["报销日期"] = df_big["开票日期"].apply(
                    lambda x: format_date_short(x) if pd.notna(x) else ""
                )
            else:
                df_big["报销日期"] = ""

            date_options = sorted(d for d in df_big["报销日期"].unique() if d)

            # 3) 让使用者自由选择要纳入本次报销的开票日期（可多选）
            if date_options:
                if len(date_options) == 1:
                    # 只有一个日期，就自动用这个
                    selected_dates = date_options
                else:
                    selected_dates = st.multiselect(
                        "选择需要汇总到本次报销伙食费明细的日期（可多选）：",
                        options=date_options,
                        default=date_options,
                    )
                    if not selected_dates:  # 如果全取消，就当作全选
                        selected_dates = date_options

                df_for_summary = df_big[df_big["报销日期"].isin(selected_dates)]
                date_text = "、".join(selected_dates)
                st.caption(f"开票日期：{date_text}")
            else:
                # 完全没有开票日期信息，就全部汇总
                df_for_summary = df_big
                st.caption("开票日期：本次上传的全部发票")

            if not df_for_summary.empty:
                # 4) 按报销项目汇总金额
                order = ["肉蛋禽", "粮油", "海鲜", "蔬菜"]
                grouped = (
                    df_for_summary.groupby("报销项目")[amount_col]
                                  .sum()
                                  .reindex(order)
                                  .fillna(0.0)
                )

                table = grouped.reset_index()
                table.columns = ["报销项目", "金额（元）"]
                table["金额（元）"] = table["金额（元）"].round(2)
                table.insert(0, "序号", range(1, len(table) + 1))

                st.dataframe(table, use_container_width=True, hide_index=True)

                food_total = table["金额（元）"].sum()
                st.write(f"**伙食费月份支出： {food_total:.2f} 元**")
            else:
                st.info("当前选定的开票日期下，没有可以归入『肉蛋禽 / 粮油 / 海鲜 / 蔬菜』的大类项目。")
        else:
            st.info("当前发票中没有可以归入『肉蛋禽 / 粮油 / 海鲜 / 蔬菜』的大类项目。")

    # ===== 分割线：下面是明细部分 =====
    st.markdown("---")

    # =========================================================
    #               明细记录（按发票分组显示）
    # =========================================================
    st.subheader("明细记录")
    
    query_text = st.text_input("搜索关键词（任意）", "", key="search_keyword")
    df_filtered = search_items(df_all, query_text)

    st.write(
        f"当前搜索关键词：`{query_text if query_text else '未输入'}`，"
        f"共 {len(df_filtered)} 条记录。"
    )

    # ---------- 搜索结果统计折叠块 ----------
    if query_text.strip():
        df_stats = df_filtered.copy()

        if "发票文件" in df_stats.columns:
            n_invoices = df_stats["发票文件"].nunique()
        else:
            n_invoices = None

        n_records = len(df_stats)

        if "数量" in df_stats.columns:
            total_qty = pd.to_numeric(df_stats["数量"], errors="coerce").sum()
        else:
            total_qty = None

        # 统计金额使用含税价
        if amount_col in df_stats.columns:
            total_amt = pd.to_numeric(df_stats[amount_col], errors="coerce").sum()
        else:
            total_amt = None

        if "单位" in df_stats.columns:
            units = df_stats["单位"].dropna().unique()
            unit_label = units[0] if len(units) == 1 else ""
        else:
            unit_label = ""
        unit_suffix = f"/{unit_label}" if unit_label else ""
        qty_suffix  = f" {unit_label}" if unit_label else ""

        if "单价" in df_stats.columns:
            price_series = pd.to_numeric(df_stats["单价"], errors="coerce")
            avg_price = price_series.mean()
            max_price = price_series.max()
            min_price = price_series.min()
        else:
            avg_price = max_price = min_price = None

        if total_qty is not None and n_records > 0:
            avg_qty_per_record = total_qty / n_records
        else:
            avg_qty_per_record = None

        with st.expander(f"「{query_text}」统计（本次上传发票）", expanded=False):
            st.write(f"- 涉及发票：{n_invoices if n_invoices is not None else '未知'} 张")
            st.write(f"- 采购记录：{n_records} 条")
            if total_qty is not None:
                st.write(f"- 总数量：{total_qty:.2f}{qty_suffix}")
                if avg_qty_per_record is not None:
                    st.write(f"- 单次平均采购数量：{avg_qty_per_record:.2f}{qty_suffix}")
            if total_amt is not None:
                st.write(f"- 总金额（含税）：{total_amt:.2f} 元")
            if avg_price is not None:
                st.write(f"- 平均单价：{avg_price:.2f} 元{unit_suffix}")
                st.write(f"- 最高单价：{max_price:.2f} 元{unit_suffix}")
                st.write(f"- 最低单价：{min_price:.2f} 元{unit_suffix}")
    
    group_cols = ["发票文件", "发票号码", "开票日期", "购买方名称", "销售方名称"]
    group_cols = [c for c in group_cols if c in df_filtered.columns]

    if not group_cols:
        st.warning("明细中没有发票抬头信息，只能直接显示商品明细。")
        detail_cols = ["类别", "商品", "单位", "数量", "单价", "金额", "税额", "含税价"]
        detail_cols = [c for c in detail_cols if c in df_filtered.columns]
        st.dataframe(
            df_filtered[detail_cols].reset_index(drop=True),
            use_container_width=True
        )
    else:
        for key, df_inv in df_filtered.groupby(group_cols, dropna=False):
            if len(group_cols) == 1:
                key = (key,)
            inv_info = dict(zip(group_cols, key))

            raw_date = inv_info.get("开票日期", "")
            date_short = format_date_short(raw_date) if raw_date else ""
            if date_short:
                expander_title = f"开票日期：{date_short}"
            elif raw_date:
                expander_title = f"开票日期：{raw_date}"
            else:
                expander_title = "开票日期：未知"

            file_name   = str(inv_info.get("发票文件", ""))
            invoice_no  = str(inv_info.get("发票号码", "")).strip()
            buyer_name  = inv_info.get("购买方名称", "")
            seller_name = inv_info.get("销售方名称", "")

            with st.expander(expander_title, expanded=False):
                if file_name:
                    st.markdown(f"**文件名：** {file_name}")
                if invoice_no:
                    st.markdown(f"**发票号码：** {invoice_no}")
                if raw_date:
                    st.markdown(f"**开票日期：** {raw_date}")
                if buyer_name:
                    st.markdown(f"**购买方：** {buyer_name}")
                if seller_name:
                    st.markdown(f"**销售方：** {seller_name}")

                st.markdown("---")

                # 明细表：去掉页码，新增 税额、含税价
                detail_cols = ["类别", "商品", "单位", "数量", "单价", "金额", "税额", "含税价"]
                detail_cols = [c for c in detail_cols if c in df_inv.columns]
                st.dataframe(
                    df_inv[detail_cols].reset_index(drop=True),
                    use_container_width=True
                )

                if query_text.strip():
                    inv_stats = df_inv.copy()

                    if "数量" in inv_stats.columns:
                        inv_total_qty = pd.to_numeric(inv_stats["数量"], errors="coerce").sum()
                    else:
                        inv_total_qty = None

                    if amount_col in inv_stats.columns:
                        inv_total_amt = pd.to_numeric(inv_stats[amount_col], errors="coerce").sum()
                    else:
                        inv_total_amt = None

                    if "单位" in inv_stats.columns:
                        units_inv = inv_stats["单位"].dropna().unique()
                        inv_unit_label = units_inv[0] if len(units_inv) == 1 else ""
                    else:
                        inv_unit_label = ""
                    inv_qty_suffix = f" {inv_unit_label}" if inv_unit_label else ""

                    if (inv_total_qty is not None) or (inv_total_amt is not None):
                        st.caption(f"本发票中「{query_text}」小结：")
                        if inv_total_qty is not None:
                            st.write(f"- 总数量：{inv_total_qty:.2f}{inv_qty_suffix}")
                        if inv_total_amt is not None:
                            st.write(f"- 总金额（含税）：{inv_total_amt:.2f} 元")

else:
    st.info("请先上传发票并点击按钮进行解析。")

st.markdown("---")

st.caption("需要新增功能请联系小何。")
