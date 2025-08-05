from flask import Flask, jsonify, render_template_string, request
from flask_restx import Api, Resource, fields
import pandas as pd
from datetime import datetime, timedelta
import google.generativeai as genai
import os
import requests
import json
import sys

# --- Cấu hình Google Gemini API ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "AIzaSyBfhSBzujVz8JbMK9H1eufrJTkhrUjS4YI")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    genai.configure(api_key="")
ai_model_text = genai.GenerativeModel('gemini-2.0-flash')

# Hàm kết nối CSDL
# import pyodbc
# def get_db_connection():
#     try:
#         # conn_str = os.getenv("DATABASE_URL")
        
#         conn = pyodbc.connect(conn_str)
#         return conn
#     except Exception as ex:
#         print(f"Lỗi kết nối CSDL: {ex}", file=sys.stderr)
#         return None
import pyodbc
import sys

def get_db_connection():
    try:
        conn_str = (
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "Server=SQL1004.site4now.net;"
            "Database=db_abbcbc_gcoffee;"
            "UID=db_abbcbc_gcoffee_admin;"
            "PWD=Thanh123@;"
            "PORT=1433"
        )
        conn = pyodbc.connect(conn_str)
        return conn
    except Exception as ex:
        print(f"Lỗi kết nối CSDL: {ex}", file=sys.stderr)
        return None

# Hàm lấy dữ liệu bán hàng
def get_sales_data_for_ai(connection):
    if connection is None:
        return pd.DataFrame()
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=7)
    query = f"""
    SELECT
        P.ProductID,
        P.ProductName,
        UOM.UnitName,
        TD.Quantity,
        T.TransactionDate,
        T.TransactionType
    FROM Transactions AS T
    JOIN TransactionDetails AS TD ON T.TransactionID = TD.TransactionID
    JOIN Products AS P ON TD.ProductID = P.ProductID
    JOIN UnitsOfMeasure AS UOM ON P.UnitOfMeasureID = UOM.UnitOfMeasureID
    WHERE
        T.TransactionType = 'Outbound'
        AND T.TransactionDate BETWEEN '{start_date.strftime('%Y-%m-%d')}' AND '{end_date.strftime('%Y-%m-%d')}'
    ORDER BY T.TransactionDate DESC, P.ProductName;
    """
    try:
        df = pd.read_sql(query, connection)
        return df
    except Exception as e:
        print(f"Lỗi khi truy vấn dữ liệu bán hàng: {e}", file=sys.stderr)
        sys.stderr.flush()
        return pd.DataFrame()

# Hàm lấy tồn kho hiện tại
def get_current_inventory(connection, product_id=None):
    if connection is None:
        return pd.DataFrame()
    query = """
    WITH CalculatedStock AS (
        SELECT
            TD.ProductID,
            SUM(CASE WHEN T.TransactionType = 'Inbound' THEN TD.Quantity ELSE 0 END) -
            SUM(CASE WHEN T.TransactionType = 'Outbound' THEN TD.Quantity ELSE 0 END) AS CalculatedQuantity
        FROM Transactions AS T
        JOIN TransactionDetails AS TD ON T.TransactionID = TD.TransactionID
        GROUP BY TD.ProductID
    ),
    RecordedStock AS (
        SELECT
            ProductID,
            Quantity AS RecordedQuantity
        FROM Inventory
    )
    SELECT
        P.ProductID,
        P.ProductName,
        UOM.UnitName,
        COALESCE(CS.CalculatedQuantity, 0) AS CalculatedStockFromTransactions,
        COALESCE(RS.RecordedQuantity, 0) AS RecordedStockInInventoryTable
    FROM Products AS P
    LEFT JOIN UnitsOfMeasure AS UOM ON P.UnitOfMeasureID = UOM.UnitOfMeasureID
    LEFT JOIN CalculatedStock AS CS ON P.ProductID = CS.ProductID
    LEFT JOIN RecordedStock AS RS ON P.ProductID = RS.ProductID
    """
    if product_id:
        query += f" WHERE P.ProductID = '{product_id}'"
    query += " ORDER BY P.ProductName;"
    try:
        df = pd.read_sql(query, connection)
        df['CurrentStock'] = df['RecordedStockInInventoryTable']
        df.loc[df['CurrentStock'].isnull() | (df['CurrentStock'] == 0), 'CurrentStock'] = df['CalculatedStockFromTransactions']
        return df
    except Exception as e:
        print(f"Lỗi khi truy vấn tồn kho: {e}", file=sys.stderr)
        sys.stderr.flush()
        return pd.DataFrame()

# Hàm đề xuất sản phẩm
def recommend_products(sales_df, inventory_df, top_n_hot=5, top_n_cold=3, target_stock_duration_weeks=2, hot_threshold_weekly=30, cold_threshold_weekly=5):
    recommendations_list = []
    if sales_df.empty:
        product_sales = pd.DataFrame(columns=['ProductID', 'ProductName', 'UnitName', 'TotalQuantitySold'])
    else:
        product_sales = sales_df.groupby(['ProductID', 'ProductName', 'UnitName']).agg(
            TotalQuantitySold=('Quantity', 'sum')
        ).reset_index()
    temp_conn_for_all_products = get_db_connection()
    all_products_df = pd.DataFrame()
    if temp_conn_for_all_products:
        try:
            all_products_query = "SELECT ProductID, ProductName, UnitOfMeasureID FROM Products;"
            all_products_df = pd.read_sql(all_products_query, temp_conn_for_all_products)
            units_df = pd.read_sql("SELECT UnitOfMeasureID, UnitName FROM UnitsOfMeasure;", temp_conn_for_all_products)
            all_products_df = pd.merge(all_products_df, units_df, on='UnitOfMeasureID', how='left')
        except Exception as e:
            print(f"Lỗi khi truy vấn tất cả sản phẩm: {e}", file=sys.stderr)
            sys.stderr.flush()
            return {
                'recommendations': [],
                'summary': "Không thể phân tích do lỗi CSDL."
            }
        finally:
            temp_conn_for_all_products.close()
    else:
        print("Lỗi kết nối CSDL tạm thời.", file=sys.stderr)
        sys.stderr.flush()
        return {
            'recommendations': [],
            'summary': "Không thể phân tích do lỗi kết nối CSDL."
        }
    merged_data = pd.merge(all_products_df, product_sales, on=['ProductID', 'ProductName', 'UnitName'], how='left')
    merged_data['TotalQuantitySold'] = merged_data['TotalQuantitySold'].fillna(0)
    merged_data = pd.merge(merged_data, inventory_df[['ProductID', 'CurrentStock']], on='ProductID', how='left')
    merged_data['CurrentStock'] = merged_data['CurrentStock'].fillna(0)
    for idx, row in merged_data.iterrows():
        product_id = row['ProductID']
        product_name = row['ProductName']
        unit_name = row['UnitName']
        quantity_sold_weekly = row['TotalQuantitySold']
        current_stock = row['CurrentStock']
        recommendation_text = ""
        recommendation_type = "Normal"
        suggested_quantity = 0
        avg_daily_sales = quantity_sold_weekly / 7 if quantity_sold_weekly > 0 else 0
        target_stock_needed = avg_daily_sales * (target_stock_duration_weeks * 7)
        if quantity_sold_weekly > hot_threshold_weekly:
            recommendation_type = "Hot"
            if target_stock_needed > current_stock:
                suggested_quantity = round(target_stock_needed - current_stock)
                recommendation_text = f"Nên nhập thêm {suggested_quantity} {unit_name} để đủ hàng {target_stock_duration_weeks} tuần."
            else:
                recommendation_text = "Hàng bán chạy. Tồn kho đủ, tiếp tục theo dõi."
        elif quantity_sold_weekly > 0 and quantity_sold_weekly < cold_threshold_weekly:
            recommendation_type = "Cold"
            if current_stock > 0 and current_stock > target_stock_needed * 0.5:
                suggested_quantity = round(current_stock - target_stock_needed)
                if suggested_quantity < 0: suggested_quantity = 0
                recommendation_text = f"Hàng bán chậm. Xem xét giảm nhập hoặc xả {current_stock} {unit_name}."
            else:
                recommendation_text = "Hàng bán chậm. Tồn kho thấp, nhập tối thiểu."
        elif quantity_sold_weekly == 0:
            recommendation_type = "Zero Sales"
            if current_stock > 0:
                recommendation_text = f"Không bán được. Ngừng nhập, xả kho {current_stock} {unit_name}."
            else:
                recommendation_text = "Không bán được và hết hàng. Ngừng nhập."
        else:
            recommendation_type = "Normal"
            if target_stock_needed > current_stock:
                suggested_quantity = round(target_stock_needed - current_stock)
                recommendation_text = f"Bán bình thường. Nhập thêm {suggested_quantity} {unit_name} để đủ {target_stock_duration_weeks} tuần."
            else:
                recommendation_text = "Bán bình thường. Tồn kho đủ."
        recommendations_list.append({
            'ProductID': product_id,
            'ProductName': product_name,
            'UnitName': unit_name,
            'TotalQuantitySoldWeekly': quantity_sold_weekly,
            'CurrentStock': current_stock,
            'RecommendationType': recommendation_type,
            'RecommendationText': recommendation_text,
            'SuggestedQuantity': suggested_quantity
        })
    def sort_key(rec_type):
        if rec_type == "Hot": return 1
        if rec_type == "Normal": return 2
        if rec_type == "Cold": return 3
        if rec_type == "Zero Sales": return 4
        return 5
    recommendations_list.sort(key=lambda x: (sort_key(x['RecommendationType']), -x['TotalQuantitySoldWeekly']))
    return {
        'recommendations': recommendations_list,
        'summary': f"Đã phân tích {len(recommendations_list)} sản phẩm."
    }

# Hàm hỗ trợ AI Chatbot
def get_product_keywords_from_db(connection):
    if connection is None: return pd.DataFrame()
    query = "SELECT ProductID, ProductName, ShortName, UnitOfMeasureID FROM Products;"
    try:
        df = pd.read_sql(query, connection)
        units_df = pd.read_sql("SELECT UnitOfMeasureID, UnitName FROM UnitsOfMeasure;", connection)
        df = pd.merge(df, units_df, on='UnitOfMeasureID', how='left')
        return df
    except Exception as e:
        print(f"Lỗi khi lấy danh sách sản phẩm: {e}", file=sys.stderr)
        sys.stderr.flush()
        return pd.DataFrame()

def find_product_by_keyword(keyword, products_df):
    if products_df.empty or not keyword: return None
    keyword_lower = keyword.lower()
    matching_product = products_df[
        (products_df['ProductName'].str.lower() == keyword_lower) |
        (products_df['ShortName'].str.lower() == keyword_lower)
    ]
    if not matching_product.empty: return matching_product.iloc[0]
    matching_product = products_df[
        products_df['ProductName'].str.lower().str.contains(keyword_lower) |
        products_df['ShortName'].str.lower().str.contains(keyword_lower)
    ]
    if not matching_product.empty: return matching_product.iloc[0]
    return None

def get_product_detailed_info(connection, product_id):
    if connection is None:
        return {}
    product_details = {}
    inventory_df = get_current_inventory(connection, product_id=product_id)
    if not inventory_df.empty:
        product_details['ProductID'] = inventory_df['ProductID'].iloc[0]
        product_details['ProductName'] = inventory_df['ProductName'].iloc[0]
        product_details['UnitName'] = inventory_df['UnitName'].iloc[0]
        product_details['CurrentStock'] = inventory_df['CurrentStock'].iloc[0]
        product_details['CalculatedStockFromTransactions'] = inventory_df['CalculatedStockFromTransactions'].iloc[0]
        product_details['RecordedStockInInventoryTable'] = inventory_df['RecordedStockInInventoryTable'].iloc[0]
    else:
        try:
            product_query = f"""
            SELECT P.ProductID, P.ProductName, UOM.UnitName
            FROM Products AS P
            LEFT JOIN UnitsOfMeasure AS UOM ON P.UnitOfMeasureID = UOM.UnitOfMeasureID
            WHERE P.ProductID = '{product_id}';
            """
            product_basic_df = pd.read_sql(product_query, connection)
            if not product_basic_df.empty:
                product_details['ProductID'] = product_basic_df['ProductID'].iloc[0]
                product_details['ProductName'] = product_basic_df['ProductName'].iloc[0]
                product_details['UnitName'] = product_basic_df['UnitName'].iloc[0]
                product_details['CurrentStock'] = 0
                product_details['CalculatedStockFromTransactions'] = 0
                product_details['RecordedStockInInventoryTable'] = 0
            else:
                return {}
        except Exception as e:
            print(f"Lỗi khi lấy thông tin sản phẩm: {e}", file=sys.stderr)
            sys.stderr.flush()
            return {}
    inbound_query = f"""
    SELECT
        T.TransactionDate,
        TD.Quantity
    FROM Transactions AS T
    JOIN TransactionDetails AS TD ON T.TransactionID = TD.TransactionID
    WHERE
        TD.ProductID = '{product_id}'
        AND T.TransactionType = 'Inbound'
    ORDER BY T.TransactionDate DESC;
    """
    inbound_transactions = []
    try:
        inbound_df = pd.read_sql(inbound_query, connection)
        for _, row in inbound_df.iterrows():
            inbound_transactions.append({
                'TransactionDate': row['TransactionDate'].strftime('%Y-%m-%d'),
                'Quantity': row['Quantity']
            })
    except Exception as e:
        print(f"Lỗi khi truy vấn lịch sử nhập hàng: {e}", file=sys.stderr)
        sys.stderr.flush()
    product_details['InboundTransactions'] = inbound_transactions
    return product_details

# Khởi tạo Flask app
app = Flask(__name__)
app.config['RESTX_MASK_SWAGGER'] = False  # Ngăn Flask-RESTx ghi đè route

# Route cho trang chính
@app.route('/', methods=['GET'])
def index_html():
    print("Route / accessed")
    return render_template_string("""
        <!DOCTYPE html>
        <html lang="vi">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Ứng Dụng Đề Xuất & Hỗ Trợ AI</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
            <style>
                body {
                    font-family: 'Inter', sans-serif;
                    background-color: #f0f4f8;
                    color: #333;
                }
                .container {
                    max-width: 960px;
                    margin: 2rem auto;
                    padding: 1.5rem;
                    background-color: #ffffff;
                    border-radius: 12px;
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
                    margin-bottom: 2rem;
                }
                h1, h2 {
                    color: #1a202c;
                    text-align: center;
                    margin-bottom: 1.5rem;
                }
                button {
                    display: block;
                    margin: 0 auto 2rem auto;
                    padding: 0.8rem 2rem;
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    font-size: 1.1rem;
                    cursor: pointer;
                    transition: background-color 0.3s ease, transform 0.2s ease;
                    box-shadow: 0 4px 8px rgba(76, 175, 80, 0.3);
                }
                button:hover {
                    background-color: #45a049;
                    transform: translateY(-2px);
                }
                button:active {
                    transform: translateY(0);
                }
                table {
                    width: 100%;
                    border-collapse: separate;
                    border-spacing: 0;
                    margin-top: 1.5rem;
                    border-radius: 8px;
                    overflow: hidden;
                }
                th, td {
                    padding: 12px 15px;
                    text-align: left;
                    border-bottom: 1px solid #e2e8f0;
                }
                th {
                    background-color: #edf2f7;
                    font-weight: 600;
                    color: #4a5568;
                    text-transform: uppercase;
                    font-size: 0.9em;
                }
                tr:last-child td {
                    border-bottom: none;
                }
                tr.hot-product { background-color: #f0fdf4; }
                tr.cold-product { background-color: #fffaf0; }
                tr.zero-sales-product { background-color: #fef2f2; }
                .loading-message {
                    text-align: center;
                    color: #718096;
                    font-style: italic;
                    margin-top: 1rem;
                }
                .ai-chat-section {
                    margin-top: 3rem;
                    padding-top: 2rem;
                    border-top: 1px solid #e2e8f0;
                }
                .chat-input-area {
                    display: flex;
                    gap: 10px;
                    margin-bottom: 1rem;
                }
                .chat-input-area textarea {
                    flex-grow: 1;
                    padding: 0.8rem;
                    border: 1px solid #cbd5e0;
                    border-radius: 8px;
                    font-size: 1rem;
                    resize: vertical;
                    min-height: 40px;
                }
                .chat-input-area button {
                    flex-shrink: 0;
                    margin: 0;
                    padding: 0.8rem 1.5rem;
                    background-color: #3182ce;
                    box-shadow: 0 4px 8px rgba(49, 130, 206, 0.3);
                }
                .chat-input-area button:hover {
                    background-color: #2c5282;
                }
                .ai-response-area {
                    background-color: #e2e8f0;
                    padding: 1rem;
                    border-radius: 8px;
                    min-height: 80px;
                    line-height: 1.6;
                    color: #2d3748;
                    white-space: pre-wrap;
                }
                .swagger-link-section {
                    text-align: center;
                    margin-top: 2rem;
                    padding-top: 1rem;
                    border-top: 1px dashed #cbd5e0;
                }
                .swagger-link {
                    display: inline-block;
                    padding: 0.6rem 1.2rem;
                    background-color: #6366f1;
                    color: white;
                    border-radius: 8px;
                    text-decoration: none;
                    font-weight: 600;
                    transition: background-color 0.3s ease;
                    box-shadow: 0 4px 8px rgba(99, 102, 241, 0.3);
                }
                .swagger-link:hover {
                    background-color: #4f46e5;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Ứng Dụng Đề Xuất & Hỗ Trợ AI</h1>
                <button onclick="getRecommendations()">Lấy Đề Xuất Mới Nhất</button>
                <div id="recommendations" class="loading-message">Nhấn nút để lấy đề xuất...</div>
                <div class="ai-chat-section">
                    <h2>Hỗ Trợ AI Hỏi Đáp Về Sản Phẩm</h2>
                    <div class="chat-input-area">
                        <textarea id="userQuery" placeholder="Ví dụ: Còn bao nhiêu cà phê Espresso?"></textarea>
                        <button onclick="askAI()">Hỏi AI</button>
                    </div>
                    <div id="aiResponse" class="ai-response-area">Câu trả lời của AI sẽ hiện ở đây.</div>
                </div>
                <div class="swagger-link-section">
                    <p>Khám phá API chi tiết tại:</p>
                    <a href="/swagger" target="_blank" class="swagger-link">Xem API Docs (Swagger UI)</a>
                </div>
            </div>
            <script>
                async function getRecommendations() {
                    const recommendationsDiv = document.getElementById('recommendations');
                    recommendationsDiv.innerHTML = '<div class="loading-message">Đang lấy dữ liệu và phân tích... Vui lòng chờ vài giây.</div>';
                    try {
                        const response = await fetch('/recommendations');
                        const data = await response.json();
                        if (response.status !== 200) {
                            recommendationsDiv.innerHTML = `<p style="color: red; text-align: center;">Lỗi từ server (${response.status}): ${data.message || 'Không rõ lỗi'}</p>`;
                            console.error("Server error:", data);
                            return;
                        }
                        let html = '<h2>Tổng Quan Đề Xuất:</h2>';
                        if (data.recommendations && data.recommendations.length > 0) {
                            html += '<table>';
                            html += '<thead><tr><th>Sản phẩm</th><th>Đã bán (tuần)</th><th>Tồn kho</th><th>Loại Đề xuất</th><th>Lời khuyên</th><th>Số lượng gợi ý</th></tr></thead><tbody>';
                            data.recommendations.forEach(p => {
                                let rowClass = '';
                                if (p.RecommendationType === 'Hot') rowClass = 'hot-product';
                                else if (p.RecommendationType === 'Cold') rowClass = 'cold-product';
                                else if (p.RecommendationType === 'Zero Sales') rowClass = 'zero-sales-product';
                                html += `<tr class="${rowClass}">
                                            <td>${p.ProductName} (${p.UnitName})</td>
                                            <td>${p.TotalQuantitySoldWeekly}</td>
                                            <td>${p.CurrentStock}</td>
                                            <td>${p.RecommendationType}</td>
                                            <td>${p.RecommendationText}</td>
                                            <td>${p.SuggestedQuantity > 0 ? p.SuggestedQuantity : '-'}</td>
                                         </tr>`;
                            });
                            html += '</tbody></table>';
                        } else {
                            html += '<p style="text-align: center;">Không có dữ liệu đề xuất hoặc lỗi trong quá trình phân tích.</p>';
                        }
                        recommendationsDiv.innerHTML = html;
                    } catch (error) {
                        recommendationsDiv.innerHTML = `<p style="color: red; text-align: center;">Đã xảy ra lỗi khi kết nối hoặc xử lý dữ liệu: ${error.message}. Vui lòng kiểm tra console để biết thêm chi tiết.</p>`;
                        console.error("Fetch error:", error);
                    }
                }
                async function askAI() {
                    const userQuery = document.getElementById('userQuery').value;
                    const aiResponseDiv = document.getElementById('aiResponse');
                    if (!userQuery.trim()) {
                        aiResponseDiv.innerText = "Vui lòng nhập câu hỏi của bạn.";
                        return;
                    }
                    aiResponseDiv.innerText = "AI đang suy nghĩ... Vui lòng chờ.";
                    try {
                        const response = await fetch('/ai/chat', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({ query: userQuery })
                        });
                        const data = await response.json();
                        if (response.ok) {
                            aiResponseDiv.innerText = data.ai_response;
                        } else {
                            aiResponseDiv.innerText = `Lỗi từ server (${response.status}): ${data.message || 'Không rõ lỗi'}`;
                            console.error("Server error:", data);
                        }
                    } catch (error) {
                        aiResponseDiv.innerText = `Đã xảy ra lỗi khi gọi AI: ${error.message}`;
                        console.error("AI Fetch error:", error);
                    }
                }
            </script>
        </body>
        </html>
    """)

# Khởi tạo Flask-RESTx API
api = Api(app, version='1.0', title='Coffee Inventory AI API',
          description='API cho Hệ thống Quản lý Tồn kho Cà phê với tính năng đề xuất và chatbot AI.',
          doc='/swagger')
reco_ns = api.namespace('recommendations', description='Operations related to product recommendations')
ai_ns = api.namespace('ai', description='AI Chat Operations')

# Định nghĩa model cho phản hồi đề xuất
reco_item_model = reco_ns.model('RecommendationItem', {
    'ProductID': fields.String(description='ID của sản phẩm'),
    'ProductName': fields.String(description='Tên sản phẩm'),
    'UnitName': fields.String(description='Đơn vị tính'),
    'TotalQuantitySoldWeekly': fields.Integer(description='Tổng số lượng bán trong tuần qua'),
    'CurrentStock': fields.Integer(description='Số lượng tồn kho hiện tại'),
    'RecommendationType': fields.String(description='Loại đề xuất (Hot, Cold, Normal, Zero Sales)'),
    'RecommendationText': fields.String(description='Văn bản đề xuất chi tiết'),
    'SuggestedQuantity': fields.Integer(description='Số lượng đề xuất nhập thêm/giảm bớt')
})

reco_response_model = reco_ns.model('RecommendationsResponse', {
    'recommendations': fields.List(fields.Nested(reco_item_model), description='Danh sách các đề xuất sản phẩm'),
    'summary': fields.String(description='Tóm tắt quá trình phân tích')
})

@reco_ns.route('/recommendations')
class Recommendations(Resource):
    @reco_ns.doc('get_product_recommendations')
    @reco_ns.marshal_with(reco_response_model)
    def get(self):
        conn = get_db_connection()
        if conn:
            try:
                sales_data_df = get_sales_data_for_ai(conn)
                inventory_df = get_current_inventory(conn)
                recommendations_data = recommend_products(sales_data_df, inventory_df,
                                                        top_n_hot=3, top_n_cold=2,
                                                        target_stock_duration_weeks=2,
                                                        hot_threshold_weekly=20,
                                                        cold_threshold_weekly=3)
                return recommendations_data, 200
            except Exception as e:
                print(f"Lỗi khi xử lý đề xuất: {e}", file=sys.stderr)
                api.abort(500, f"Lỗi trong quá trình xử lý đề xuất: {str(e)}")
            finally:
                if conn:
                    conn.close()
        else:
            api.abort(500, "Không thể kết nối CSDL để lấy đề xuất.")

# Định nghĩa models cho AI chatbot
ai_query_model = ai_ns.model('AIQuery', {
    'query': fields.String(required=True, description='Câu hỏi của người dùng cho chatbot AI')
})

ai_response_model = ai_ns.model('AIResponse', {
    'ai_response': fields.String(required=True, description='Phản hồi từ chatbot AI')
})

@ai_ns.route('/chat')
class AIChat(Resource):
    @ai_ns.doc('ask_ai_chatbot')
    @ai_ns.expect(ai_query_model, validate=True)
    @ai_ns.marshal_with(ai_response_model)
    def post(self):
        print("\n--- Yêu cầu /ai/chat nhận được ---", file=sys.stdout)
        sys.stdout.flush()
        user_query = api.payload.get('query', '')
        if not user_query:
            api.abort(400, "Vui lòng cung cấp câu hỏi.")
        conn = get_db_connection()
        if not conn:
            print("Lỗi: Không thể kết nối CSDL.", file=sys.stderr)
            sys.stdout.flush()
            api.abort(500, "Không thể kết nối CSDL.")
        try:
            print(f"Bắt đầu lấy danh sách sản phẩm. Query: '{user_query}'", file=sys.stdout)
            sys.stdout.flush()
            products_for_ai = get_product_keywords_from_db(conn)
            print(f"Đã lấy {len(products_for_ai)} sản phẩm.", file=sys.stdout)
            sys.stdout.flush()
            product_names_list = []
            if not products_for_ai.empty:
                for _, row in products_for_ai.iterrows():
                    product_names_list.append(row['ProductName'])
                    if row['ShortName']:
                        product_names_list.append(row['ShortName'])
                product_names_string = ", ".join(list(set(product_names_list)))
            else:
                product_names_string = "không có sản phẩm nào"
            print(f"AI xác định từ khóa. Products: {product_names_string[:100]}...", file=sys.stdout)
            sys.stdout.flush()
            prompt_product_extraction = f"""
            Bạn là một trợ lý AI giúp xác định sản phẩm từ câu hỏi.
            Từ câu hỏi: "{user_query}", xác định từ khóa sản phẩm.
            Danh sách sản phẩm: {product_names_string}.
            Trả về JSON: {{'product_keyword': null}} nếu không tìm thấy.
            """
            try:
                payload = {
                    "contents": [{"role": "user", "parts": [{"text": prompt_product_extraction}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseSchema": {
                            "type": "OBJECT",
                            "properties": {
                                "product_keyword": {"type": "STRING", "nullable": True}
                            }
                        }
                    }
                }
                apiUrl = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GOOGLE_API_KEY}"
                response_gemini = requests.post(apiUrl, json=payload)
                response_gemini.raise_for_status()
                content_type = response_gemini.headers.get('Content-Type', '')
                if 'application/json' not in content_type:
                    print(f"Phản hồi từ Gemini không phải JSON: {response_gemini.text}", file=sys.stderr)
                    api.abort(500, "Lỗi: Phản hồi AI không hợp lệ.")
                gemini_response_json = response_gemini.json()
                product_keyword = None
                if gemini_response_json and gemini_response_json.get('candidates'):
                    parts = gemini_response_json['candidates'][0]['content']['parts']
                    if parts and parts[0].get('text'):
                        parsed_text = json.loads(parts[0]['text'])
                        product_keyword = parsed_text.get('product_keyword')
            except Exception as e:
                print(f"Lỗi khi gọi Gemini API: {e}", file=sys.stderr)
                sys.stderr.flush()
                api.abort(500, f"Lỗi khi gọi AI: {e}")
            ai_response_text = "Không tìm thấy thông tin sản phẩm. Hỏi rõ hơn?"
            if product_keyword:
                found_product = find_product_by_keyword(product_keyword, products_for_ai)
                if found_product is not None:
                    detailed_product_info = get_product_detailed_info(conn, product_id=found_product['ProductID'])
                    if detailed_product_info:
                        product_id = detailed_product_info['ProductID']
                        product_name = detailed_product_info['ProductName']
                        unit_name = detailed_product_info['UnitName']
                        current_stock = detailed_product_info['CurrentStock']
                        inbound_transactions = detailed_product_info['InboundTransactions']
                        prompt_response_generation = f"""
                        Bạn là trợ lý tồn kho thân thiện.
                        Câu hỏi: "{user_query}"
                        Sản phẩm: "{product_name}" (Mã: {product_id}).
                        Thông tin:
                        - Tên: {product_name}
                        - Đơn vị: {unit_name}
                        - Tồn kho: {current_stock} {unit_name}
                        - Mã: {product_id}
                        - Lịch sử nhập:
                        """
                        if inbound_transactions:
                            for tx in inbound_transactions[:3]:
                                prompt_response_generation += f"\n  - Ngày: {tx['TransactionDate']}, Số lượng: {tx['Quantity']} {unit_name}"
                        else:
                            prompt_response_generation += "\n  - Không có giao dịch nhập gần đây."
                        prompt_response_generation += f"""
                        Trả lời ngắn gọn, lịch sự, dùng thông tin trên.
                        """
                        try:
                            chat_history = [{"role": "user", "parts": [{"text": prompt_response_generation}]}]
                            payload = {"contents": chat_history}
                            apiUrl = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GOOGLE_API_KEY}"
                            response_gen = requests.post(apiUrl, json=payload)
                            response_gen.raise_for_status()
                            content_type = response_gen.headers.get('Content-Type', '')
                            if 'application/json' not in content_type:
                                print(f"Phản hồi từ Gemini không phải JSON: {response_gen.text}", file=sys.stderr)
                                ai_response_text = (
                                    f"Sản phẩm: {product_name} (Mã: {product_id})\n"
                                    f"Tồn kho: {current_stock} {unit_name}\n"
                                    f"(Lỗi: Phản hồi AI không hợp lệ.)"
                                )
                            else:
                                result_gen = response_gen.json()
                                if result_gen.get('candidates') and result_gen['candidates'][0].get('content') and result_gen['candidates'][0]['content'].get('parts'):
                                    ai_response_text = result_gen['candidates'][0]['content']['parts'][0]['text']
                                else:
                                    ai_response_text = (
                                        f"Sản phẩm: {product_name} (Mã: {product_id})\n"
                                        f"Tồn kho: {current_stock} {unit_name}\n"
                                        f"(Không nhận được phản hồi từ AI.)"
                                    )
                        except Exception as e:
                            print(f"Lỗi khi gọi Gemini API: {e}", file=sys.stderr)
                            sys.stderr.flush()
                            ai_response_text = (
                                f"Sản phẩm: {product_name} (Mã: {product_id})\n"
                                f"Tồn kho: {current_stock} {unit_name}\n"
                                f"(Lỗi khi gọi AI: {e})"
                            )
                    else:
                        ai_response_text = f"Tìm thấy '{product_name}' nhưng không lấy được thông tin chi tiết."
                else:
                    ai_response_text = f"Không tìm thấy '{product_keyword}'. Thử tên khác?"
            else:
                ai_response_text = "Không xác định được sản phẩm. Hỏi rõ hơn?"
        except Exception as e:
            print(f"Lỗi trong /ai/chat: {e}", file=sys.stderr)
            sys.stdout.flush()
            api.abort(500, f"Lỗi hệ thống: {str(e)}")
        finally:
            if conn:
                conn.close()
        return {'ai_response': ai_response_text}, 200
# Thêm vào cuối file AI.py, trước dòng if __name__ == '__main__':

# Global error handlers
@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        'error': 'Internal server error',
        'message': 'Đã xảy ra lỗi hệ thống. Vui lòng thử lại sau.'
    }), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        'error': 'Not found',
        'message': 'Endpoint không tồn tại'
    }), 404

# Health check endpoint
@app.route('/health')
def health_check():
    db_status = "ERROR"
    db_message = "Không thể kết nối"
    
    try:
        conn = get_db_connection()
        if conn:
            db_status = "OK"
            db_message = "Kết nối thành công"
            conn.close()
    except Exception as e:
        db_message = f"Lỗi: {str(e)}"
    
    return jsonify({
        'status': 'OK',
        'database': {
            'status': db_status,
            'message': db_message,
            'available': DB_AVAILABLE
        },
        'timestamp': datetime.now().isoformat()
    })

# Cập nhật app configuration
app.config['JSON_AS_ASCII'] = False  # Hỗ trợ tiếng Việt
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True
# Chạy ứng dụng
# if __name__ == '__main__':
#     app.run(debug=True, host='0.0.0.0', port=os.environ.get('PORT', 5000))