from docling.document_converter import DocumentConverter
from docling_core.types.doc import DocItemLabel
import pandas as pd
import io
import gradio as gr
import os
import fitz  # PyMuPDF
from PIL import Image
from datetime import datetime
from google import genai
from google.genai import types
from dotenv import load_dotenv, find_dotenv
import pinecone
from pinecone import Pinecone
import cohere

_= load_dotenv(find_dotenv())

def get_pdf_pages(file_path):
    if not file_path or not os.path.exists(file_path):
        return []
    
    try:
        doc = fitz.open(file_path)
        pages = []
        
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            mat = fitz.Matrix(1.5, 1.5)  # 1.5x zoom
            pix = page.get_pixmap(matrix=mat)
            img_data = pix.tobytes("png")
            
            img = Image.open(io.BytesIO(img_data))
            pages.append(img)
        
        doc.close()
        return pages
    except Exception as e:
        print(f"PDF sayfa oluşturma hatası: {e}")
        return []

def process_pdf(file_path):
    if not file_path or not os.path.exists(file_path):
        return pd.DataFrame(), "PDF dosyası bulunamadı"
    
    try:
        converter = DocumentConverter()
        doc = converter.convert(file_path).document
        
        markdown_content = doc.export_to_markdown(
            labels={DocItemLabel.TABLE},  # Sadece tabloları dahil et
            image_placeholder="",  # Resim placeholder'ını boş bırak
            enable_chart_tables=False,  # Grafik tablolarını devre dışı bırak
        )
        
        temp_dir = "temp"
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        
        temp_md_file = os.path.join(temp_dir, "temp_tables.md")
        with open(temp_md_file, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        
        df = markdown_to_dataframe(temp_md_file)
        
        if os.path.exists(temp_md_file):
            os.remove(temp_md_file)
        
        return df, "PDF başarıyla işlendi"
        
    except Exception as e:
        return pd.DataFrame(), f"Hata: {str(e)}"

def merge_tables(markdown_text):
    lines = markdown_text.split('\n')
    table_lines = []
    header_found = False
    
    for line in lines:
        if line.strip().startswith('|') and line.strip().endswith('|'):
            if '---' not in line:
                if not header_found:
                    table_lines.append(line)
                    header_found = True
                else:
                    # Eğer sütun sayısı aynıysa ve içerik başlık gibi görünüyorsa atla
                    if line.count('|') == table_lines[0].count('|'):
                        continue
                    else:
                        table_lines.append(line)
    
    if table_lines:
        header = table_lines[0]
        data_rows = table_lines[1:]
        
        merged_table = header + '\n'
        merged_table += '|' + '---|' * (header.count('|') - 1) + '\n'
        merged_table += '\n'.join(data_rows)
        
        return merged_table
    else:
        return markdown_text

def markdown_to_dataframe(markdown_file):
    with open(markdown_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    all_data_rows = []
    header = None
    
    for line in lines:
        if line.strip().startswith('|') and line.strip().endswith('|') and '---' not in line:
            if header is None:
                header = line
            else:
                if not any(word in line.lower() for word in ['tarih', 'tahlil', 'sonuç', 'birimi', 'referans']):
                    all_data_rows.append(line)
    
    if header and all_data_rows:
        full_content = header + '\n'
        full_content += '|' + '---|' * (header.count('|') - 1) + '\n'
        full_content += '\n'.join(all_data_rows)
        
        df = pd.read_csv(io.StringIO(full_content), sep='|')
        df = df.iloc[:, 1:-1]
        df.columns = df.columns.str.strip()
        df = df.dropna(how='all')
        df = df[~df.astype(str).apply(lambda x: x.str.contains('---', na=False)).any(axis=1)]
        
        return df
    else:
        return pd.DataFrame()

def get_gemini_embedding(text):
    try:
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        
        result = client.models.embed_content(
            model="gemini-embedding-001",
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=768)
        )
        
        [embedding_obj] = result.embeddings
        return embedding_obj.values
        
    except Exception as e:
        print(f"Gemini embedding oluşturma hatası: {e}")
        return None

def load_and_index_lab_reference():
    lab_data = {}
    lab_dir = "kan_tahlili"
    
    if not os.path.exists(lab_dir):
        return lab_data
    
    try:
        pc = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
        index_name = "kan-tahlili-reference"
        
        try:
            index = pc.Index(index_name)
        except:
            pc.create_index(
                name=index_name,
                dimension=768,  # Gemini embedding boyutu
                metric="cosine"
            )
            index = pc.Index(index_name)
        
        for filename in os.listdir(lab_dir):
            if filename.endswith('.txt'):
                lab_name = filename.replace('.txt', '')
                file_path = os.path.join(lab_dir, filename)
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    lab_data[lab_name] = content
                    
                    # Gemini embedding kullan
                    embedding = get_gemini_embedding(content)
                    
                    if embedding is not None:
                        ascii_id = lab_name.encode('ascii', 'ignore').decode('ascii')
                        if not ascii_id:
                            ascii_id = f"lab_{hash(lab_name) % 10000}"
                        
                        index.upsert(vectors=[{
                            'id': ascii_id,
                            'values': embedding,
                            'metadata': {
                                'lab_name': lab_name,
                                'content': content
                            }
                        }])
                    
    except Exception as e:
        print(f"Kan tahlili verileri yüklenirken hata: {e}")
    
    return lab_data

def detect_abnormal_values(df):
    abnormal_values = []
    
    for index, row in df.iterrows():
        for col in df.columns:
            value = str(row[col]).strip()
            if value and value != 'nan':
                try:
                    numeric_value = float(value)
                    abnormal_values.append({
                        'test_name': col,
                        'value': numeric_value,
                        'row': index
                    })
                except:
                    if any(keyword in value.lower() for keyword in ['yüksek', 'düşük', 'artmış', 'azalmış', 'pozitif', 'negatif']):
                        abnormal_values.append({
                            'test_name': col,
                            'value': value,
                            'row': index
                        })
    
    return abnormal_values

def get_relevant_references(abnormal_values):
    if not abnormal_values:
        return ""
    
    try:
        pc = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
        index = pc.Index("kan-tahlili-reference")
        
        co = cohere.ClientV2(api_key=os.environ.get("COHERE_API_KEY"))
        
        query_text = " ".join([f"{item['test_name']} {item['value']}" for item in abnormal_values])
        
        # Gemini embedding kullan
        query_embedding = get_gemini_embedding(query_text)
        
        if query_embedding is None:
            return ""
        
        search_results = index.query(
            vector=query_embedding,
            top_k=20,
            include_metadata=True
        )
        
        if search_results['matches']:
            documents = [match['metadata']['content'] for match in search_results['matches']]
            rerank_results = co.rerank(
                model="rerank-v3.5",
                query=query_text,
                documents=documents,
                top_n=10
            )
            
            relevant_refs = []
            for result in rerank_results.results:
                if result.relevance_score > 0.7:
                    relevant_refs.append(documents[result.index])
            
            return "\n\n".join(relevant_refs)
        
    except Exception as e:
        print(f"Referans arama hatası: {e}")
    
    return ""

def analyze_with_gemini(df):
    if df.empty:
        return "Analiz edilecek veri bulunamadı."
    
    try:
        markdown_content = df.to_markdown(index=False)
        
        abnormal_values = detect_abnormal_values(df)
        
        relevant_references = get_relevant_references(abnormal_values)
        
        client = genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY"),
        )
        
        analysis_prompt = f"""
        Aşağıdaki tablo verilerini analiz et ve fazla ya da düşük değerleri tespit et:
        
        {markdown_content}
        
        Lütfen şunları analiz et:
        1. Hangi değerler normal aralığın dışında?
        2. Hangi değerler yüksek risk taşıyor?
        3. Hangi değerler düşük risk taşıyor?
        4. Genel sağlık durumu hakkında öneriler?
        5. Anormal değerler için spesifik tedavi önerileri?
        
        {relevant_references if relevant_references else ""}
        
        Yukarıdaki referans bilgileri kullanarak, anormal değerler için spesifik nedenler ve tedavi önerileri sun. Her anormal değer için:
        - Olası nedenleri
        - Nasıl düzeltileceği
        - Hangi doktora başvurulması gerektiği
        - Yaşam tarzı değişiklikleri
        
        Türkçe olarak detaylı bir analiz raporu hazırla.
        """
        
        model = "gemini-2.5-flash"
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=analysis_prompt),
                ],
            ),
        ]
        generate_content_config = types.GenerateContentConfig(
            thinking_config = types.ThinkingConfig(
                thinking_budget=0,
            ),
        )

        analysis_result = ""
        for chunk in client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=generate_content_config,
        ):
            analysis_result += chunk.text
        
        return analysis_result
        
    except Exception as e:
        return f"Analiz sırasında hata oluştu: {str(e)}"

def update_interface(pdf_file):
    if pdf_file is None:
        return None, pd.DataFrame()
    
    df, message = process_pdf(pdf_file)
    
    return pdf_file, df

def create_interface():
    with gr.Blocks(title="Publica - Tıbbi Rapor Analizi", theme=gr.themes.Soft(), css="""
        .pdf-nav-row {
            display: flex !important;
            flex-direction: row !important;
            width: 100% !important;
            justify-content: center !important;
            align-items: center !important;
            gap: 10px !important;
        }
        
        .pdf-page-info {
            margin: 0 10px !important;
            font-family: monospace !important;
            text-align: center !important;
            min-width: 60px !important;
            display: flex !important;
            justify-content: center !important;
            align-items: center !important;
            flex: 1 !important;
            max-width: 80px !important;
            width: 100% !important;
        }
        
        .pdf-page-info input {
            text-align: center !important;
            width: 100% !important;
            border: none !important;
            background: transparent !important;
            padding: 0 !important;
            margin: 0 !important;
            flex: 1 !important;
        }
        
        .pdf-page-info .processing {
            display: none !important;
        }
        
        .pdf-nav-row button {
            min-width: 40px !important;
            height: 32px !important;
        }
        
        .analysis-result {
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            border: 1px solid #dee2e6 !important;
            border-radius: 12px !important;
            padding: 20px !important;
            margin: 10px 0 !important;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1) !important;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
            line-height: 1.6 !important;
        }
        
        .analysis-result h1, .analysis-result h2, .analysis-result h3 {
            color: #2c3e50 !important;
            margin-top: 20px !important;
            margin-bottom: 10px !important;
        }
        
        .analysis-result h1:first-child, .analysis-result h2:first-child, .analysis-result h3:first-child {
            margin-top: 0 !important;
        }
        
        .analysis-result p {
            margin-bottom: 15px !important;
            color: #495057 !important;
        }
        
        .analysis-result * {
            color: #495057 !important;
        }
        
        .analysis-result h1, .analysis-result h2, .analysis-result h3, .analysis-result h4, .analysis-result h5, .analysis-result h6 {
            color: #2c3e50 !important;
        }
        
        .analysis-result strong, .analysis-result b {
            color: #2c3e50 !important;
            font-weight: 600 !important;
        }
        
        .analysis-result em, .analysis-result i {
            color: #495057 !important;
        }
        
        .analysis-result code {
            background: #f8f9fa !important;
            color: #2c3e50 !important;
            padding: 2px 4px !important;
            border-radius: 3px !important;
        }
        
        .analysis-result ul, .analysis-result ol {
            margin-bottom: 15px !important;
            padding-left: 25px !important;
        }
        
        .analysis-result li {
            margin-bottom: 8px !important;
            color: #495057 !important;
        }
        
        .analysis-result strong {
            color: #2c3e50 !important;
            font-weight: 600 !important;
        }
        
        .analysis-result blockquote {
            border-left: 4px solid #007bff !important;
            padding-left: 15px !important;
            margin: 15px 0 !important;
            background: rgba(0, 123, 255, 0.05) !important;
            border-radius: 0 8px 8px 0 !important;
        }
        
        .analysis-result table {
            background: #ffffff !important;
            border: 1px solid #dee2e6 !important;
            border-collapse: collapse !important;
            width: 100% !important;
            margin: 15px 0 !important;
        }
        
        .analysis-result th, .analysis-result td {
            border: 1px solid #dee2e6 !important;
            padding: 8px 12px !important;
            text-align: left !important;
            background: #ffffff !important;
            color: #495057 !important;
        }
        
        .analysis-result th {
            background: #f8f9fa !important;
            font-weight: 600 !important;
            color: #2c3e50 !important;
        }
        
        .analysis-result tr:nth-child(even) {
            background: #f8f9fa !important;
        }
        
        .analysis-result tr:nth-child(odd) {
            background: #ffffff !important;
        }
        
        .analysis-title {
            text-align: center !important;
            margin-bottom: 0 !important;
        }
    """) as demo:
        gr.Markdown("# 🏥 Publica - Tıbbi Rapor Analizi")
        gr.Markdown("Laboratuvar raporlarınızı yükleyin ve anormal değerleri otomatik olarak tespit edin.")
        
        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown("## 📖 Laboratuvar Raporu Önizleme")
                pdf_preview = gr.File(
                    label="Laboratuvar Raporu Seçin",
                    file_types=[".pdf"],
                    type="filepath"
                )
                
                pdf_display = gr.Image(
                    label="Rapor Sayfası",
                    type="pil",
                    height=600
                )
                
                with gr.Row(equal_height=True, elem_classes="pdf-nav-row"):
                    prev_btn = gr.Button("⬅️", size="sm", scale=1, interactive=False)
                    page_info = gr.Textbox(
                        value="- / -",
                        interactive=False,
                        scale=1,
                        show_label=False,
                        container=False,
                        elem_classes="pdf-page-info"
                    )
                    next_btn = gr.Button("➡️", size="sm", scale=1, interactive=False)
                
                pdf_pages = gr.State([])
                current_page = gr.State(0)
            
            with gr.Column(scale=2):
                gr.Markdown("## 📊 Laboratuvar Değerleri")
                
                process_btn = gr.Button(
                    "🔍 Değerleri Çıkar",
                    variant="primary",
                    size="lg"
                )
                
                dataframe_display = gr.Dataframe(
                    label="Laboratuvar Değerleri",
                    headers=["Test", "Değer", "Birim"],
                    datatype=["str", "str", "str"],
                    interactive=False,
                    wrap=True
                )
                
                csv_download = gr.DownloadButton(
                    label="📥 CSV Olarak İndir",
                    visible=False
                )
        
                analyze_btn = gr.Button(
                    "🏥 Tıbbi Analiz Yap",
                    variant="secondary",
                    size="lg",
                    visible=False
                )
        
        with gr.Row():
            with gr.Column():
                with gr.Group():
                    gr.Markdown("## 🏥 Tıbbi Analiz Raporu", elem_classes="analysis-title")
                    analysis_result = gr.Markdown(
                        value="",
                        visible=False,
                        elem_classes="analysis-result"
                    )
        
        def on_pdf_upload(pdf_file):
            if pdf_file is None:
                return (
                    None,  # PDF görüntü
                    [],  # PDF sayfaları
                    0,  # Mevcut sayfa
                    "- / -",  # Sayfa bilgisi
                    gr.update(interactive=False),  # Sol ok disabled
                    gr.update(interactive=False)  # Sağ ok disabled
                )
            
            pages = get_pdf_pages(pdf_file)
            
            if not pages:
                return (
                    None,
                    [],
                    0,
                    "- / -",
                    gr.update(interactive=False),
                    gr.update(interactive=False)
                )

            first_page = pages[0]
            page_info_text = f"1 / {len(pages)}"
            
            return (
                first_page,
                pages,
                0,
                page_info_text,
                gr.update(interactive=False),
                gr.update(interactive=len(pages) > 1)
            )
        
        def extract_tables(pdf_file):
            if pdf_file is None:
                return (
                    pd.DataFrame(), 
                    gr.update(visible=False),
                    gr.update(visible=False),
                    gr.update(visible=False)
                )
            
            df, message = process_pdf(pdf_file)
            
            csv_file = None
            if not df.empty:
                temp_dir = "temp"
                if not os.path.exists(temp_dir):
                    os.makedirs(temp_dir)
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_filename = f"{timestamp}.csv"
                csv_file = os.path.join(temp_dir, csv_filename)
                df.to_csv(csv_file, index=False, encoding='utf-8-sig')
            
            return (
                df,
                gr.update(visible=not df.empty, value=csv_file),
                gr.update(visible=not df.empty),
                gr.update(visible=False)
            )
        
        def analyze_data(df):
            if df.empty:
                return (
                    gr.update(visible=False, value="Analiz edilecek veri bulunamadı."),
                    gr.update(interactive=True, value="🏥 Tıbbi Analiz Yap")
                )
            
            analysis = analyze_with_gemini(df)
            
            formatted_analysis = f"""
## 🏥 Tıbbi Analiz Raporu

{analysis}
            """
            
            return (
                gr.update(visible=True, value=formatted_analysis),
                gr.update(interactive=True, value="🔍 Analiz Et")
            )
        
        def start_analysis(df):
            if df.empty:
                return (
                    gr.update(visible=False, value="Analiz edilecek veri bulunamadı."),
                    gr.update(interactive=False, value="⏳ Tıbbi Analiz Yapılıyor...")
                )
            
            return (
                gr.update(visible=True, value="⏳ Tıbbi analiz yapılıyor, lütfen bekleyin..."),
                gr.update(interactive=False, value="⏳ Tıbbi Analiz Yapılıyor...")
            )
        
        def go_to_previous_page(pages, current):
            if not pages or current <= 0:
                return gr.update()
            new_page = current - 1
            return (
                pages[new_page],  # PDF görüntü
                new_page,  # Mevcut sayfa
                f"{new_page + 1} / {len(pages)}",  # Sayfa bilgisi
                gr.update(interactive=new_page > 0),  # Sol ok durumu
                gr.update(interactive=new_page < len(pages) - 1)  # Sağ ok durumu
            )
        
        def go_to_next_page(pages, current):
            if not pages or current >= len(pages) - 1:
                return gr.update()
            new_page = current + 1
            return (
                pages[new_page],  # PDF görüntü
                new_page,  # Mevcut sayfa
                f"{new_page + 1} / {len(pages)}",  # Sayfa bilgisi
                gr.update(interactive=new_page > 0),  # Sol ok durumu
                gr.update(interactive=new_page < len(pages) - 1)  # Sağ ok durumu
            )
        
        pdf_preview.change(
            fn=on_pdf_upload,
            inputs=[pdf_preview],
            outputs=[pdf_display, pdf_pages, current_page, page_info, prev_btn, next_btn]
        )
        
        process_btn.click(
            fn=extract_tables,
            inputs=[pdf_preview],
            outputs=[dataframe_display, csv_download, analyze_btn, analysis_result]
        )
        
        analyze_btn.click(
            fn=start_analysis,
            inputs=[dataframe_display],
            outputs=[analysis_result, analyze_btn]
        ).then(
            fn=analyze_data,
            inputs=[dataframe_display],
            outputs=[analysis_result, analyze_btn]
        )
        
        prev_btn.click(
            fn=go_to_previous_page,
            inputs=[pdf_pages, current_page],
            outputs=[pdf_display, current_page, page_info, prev_btn, next_btn]
        )
        
        next_btn.click(
            fn=go_to_next_page,
            inputs=[pdf_pages, current_page],
            outputs=[pdf_display, current_page, page_info, prev_btn, next_btn]
        )
    
    return demo

def test_gemini_embedding():
    """Gemini embedding fonksiyonunu test eder"""
    print("Gemini embedding test ediliyor...")
    test_text = "Hemoglobin değeri yüksek"
    embedding = get_gemini_embedding(test_text)
    
    if embedding:
        print(f"✅ Gemini embedding başarılı! Boyut: {len(embedding)}")
        return True
    else:
        print("❌ Gemini embedding başarısız!")
        return False

if __name__ == "__main__":
    print("Gemini embedding test ediliyor...")
    if test_gemini_embedding():
        print("Kan tahlili referans bilgileri yükleniyor...")
        load_and_index_lab_reference()
        print("Referans bilgileri başarıyla yüklendi.")
        
        demo = create_interface()
        demo.launch(
            server_name="0.0.0.0",
            server_port=7860,
            share=False,
            show_error=True
        )
    else:
        print("Gemini embedding testi başarısız. Lütfen API anahtarınızı kontrol edin.")