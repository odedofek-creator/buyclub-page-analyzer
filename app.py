import streamlit as st
import google.generativeai as genai
from tavily import TavilyClient
from bs4 import BeautifulSoup
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import PyPDF2
import pandas as pd
from datetime import datetime
import io

# ==============================================================================
# CONFIGURATION & SETUP
# ==============================================================================

st.set_page_config(page_title="BuyClub Page Analyzer", layout="wide", page_icon="🛡️")

# Verify Secrets
required_secrets = ["GOOGLE_API_KEY", "TAVILY_API_KEY", "gcp_service_account"]
if not all(k in st.secrets for k in required_secrets):
    st.error("🚨 Missing API Keys in .streamlit/secrets.toml")
    st.stop()

# Initialize APIs
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
tavily = TavilyClient(api_key=st.secrets["TAVILY_API_KEY"])

# Google Sheets Connector
@st.cache_resource
def init_google_sheets():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(dict(st.secrets["gcp_service_account"]), scope)
        client = gspread.authorize(creds)
        # Attempt to open the brain sheet
        sheet = client.open("BuyClub_Page_Analyzer_Brain")
        return sheet
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        return None

sh = init_google_sheets()

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def scrape_url(url):
    """Basic text extraction from a URL."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Kill all script and style elements
        for script in soup(["script", "style", "nav", "footer"]):
            script.extract()
            
        text = soup.get_text(separator="\n")
        return "\n".join([line.strip() for line in text.splitlines() if line.strip()])
    except Exception as e:
        return f"Error scraping URL: {e}"

def extract_text_from_file(uploaded_file):
    """Extracts text from PDF or TXT."""
    if uploaded_file is None:
        return ""
    
    text = ""
    try:
        if uploaded_file.type == "application/pdf":
            reader = PyPDF2.PdfReader(uploaded_file)
            for page in reader.pages:
                text += page.extract_text() + "\n"
        elif uploaded_file.type == "text/plain":
            text = uploaded_file.read().decode("utf-8")
        else:
            text = "[Image/Unsupported File Uploaded - Content not readable by this script version]"
    except Exception as e:
        text = f"Error reading file: {e}"
    return text

def get_rules(sheet_obj, category):
    """Fetches General Rules, Specific Category Rules, and Feedback."""
    if not sheet_obj:
        return "", "", ""
    
    try:
        # 1. General Rules
        ws_gen = sheet_obj.worksheet("General_Rules")
        gen_rules = "\n".join([r[0] for r in ws_gen.get_all_values() if r])

        # 2. Category Rules
        ws_cat = sheet_obj.worksheet("Category_Rules")
        cat_data = ws_cat.get_all_values()
        headers = cat_data[0]
        
        cat_rules_text = ""
        if category in headers:
            col_index = headers.index(category)
            # Get data from that specific column, skipping header
            rules = [row[col_index] for row in cat_data[1:] if len(row) > col_index and row[col_index].strip()]
            cat_rules_text = "\n".join(rules)
        else:
            cat_rules_text = "No specific rules found for this category."

        # 3. Feedback Log
        ws_feed = sheet_obj.worksheet("Feedback_Log")
        feed_rules = "\n".join([r[0] for r in ws_feed.get_all_values() if r])

        return gen_rules, cat_rules_text, feed_rules

    except Exception as e:
        st.error(f"Error fetching rules: {e}")
        return "", "", ""

def perform_research(query):
    """Uses Tavily to search for factual verification."""
    try:
        # Optimize query for Geneva context if not specified
        if "geneva" not in query.lower() and "lausanne" not in query.lower():
            query += " Switzerland business reviews official site"
            
        response = tavily.search(query=query, search_depth="advanced", max_results=5)
        
        context_data = []
        for result in response.get('results', []):
            context_data.append(f"Source: {result['url']}\nTitle: {result['title']}\nSnippet: {result['content']}\n")
            
        return "\n".join(context_data)
    except Exception as e:
        return f"Search failed: {e}"

def analyze_with_gemini(scraped_txt, prev_txt, contract_txt, search_data, gen_rules, cat_rules, feed_log, specific_instr):
    
    system_prompt = """
    You are a strict Compliance Officer for 'BuyClub'. 
    Your Core Directive: Verify accuracy, enforce consistency, and identify marketing opportunities.
    
    INPUTS:
    1. Input text may be in French or English.
    2. TRANSLATE all internal logic to English.
    3. FINAL OUTPUT must be in English.
    
    TONE:
    - Clinical, concise, factual. 
    - No pleasantries (e.g., "Here is the report"). Start immediately with the data.
    - Zero Hallucinations: If a marketing claim is made based on external data, you MUST provide the URL using format `[Source](url)`.
    
    ANALYSIS STRUCTURE:
    
    1. 📊 Executive Summary
       - Score (0-100) based on severity of errors.
       - One-Line Verdict.
       
    2. 🚨 Section 1: Critical Issues
       - Contract Mismatches: Compare Contract Text vs Page Text (Price, Dates, Conditions).
       - Regression: Compare Previous Deal vs Current Page (Did we lose a key selling point? Is the discount lower?).
       - Factual Errors: Compare Page Text vs Search Data.
       
    3. ⚠️ Section 2: Compliance & Quality
       - Rules Broken: Check against General Rules, Category Rules, and Feedback Log.
       - Spelling/Grammar issues.
       
    4. 💡 Section 3: Marketing Opportunities
       - Awards/Reviews found in Search Data that are missing from the page. (MUST include Source Link).
       - Copy improvements for clarity or sales impact.
    """

    user_prompt = f"""
    **DATA FOR ANALYSIS**
    
    [CATEGORY RULES]: {cat_rules}
    [GENERAL RULES]: {gen_rules}
    [FEEDBACK LOG]: {feed_log}
    [SPECIFIC INSTRUCTIONS]: {specific_instr}
    
    [CONTRACT TEXT]: {contract_txt}
    
    [PREVIOUS DEAL TEXT]: {prev_txt}
    
    [CURRENT PAGE TEXT (TARGET)]: {scraped_txt}
    
    [EXTERNAL SEARCH RESEARCH]: {search_data}
    """

    # FIX APPLIED: Using system_instruction correctly as per Claude's advice
    model = genai.GenerativeModel(
        model_name='gemini-1.5-flash',
        system_instruction=system_prompt
    )
    
    response = model.generate_content(user_prompt)
    return response.text

def archive_report(sheet_obj, deal_name, category, report_text):
    """Parses score and saves full report to Archive tab."""
    try:
        ws = sheet_obj.worksheet("Analysis_Archive")
        
        # Extract score roughly (looking for "Score: XX" or similar)
        import re
        score_match = re.search(r"Score.*?(\d{1,3})", report_text)
        score = score_match.group(1) if score_match else "N/A"
        
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            deal_name,
            category,
            score,
            report_text
        ])
    except Exception as e:
        st.error(f"Archiving failed: {e}")

def save_feedback_rule(sheet_obj, rule_text):
    try:
        ws = sheet_obj.worksheet("Feedback_Log")
        ws.append_row([rule_text, datetime.now().strftime("%Y-%m-%d")])
        st.success("Rule learned and saved to Feedback Log.")
    except Exception as e:
        st.error(f"Failed to save rule: {e}")

# ==============================================================================
# UI LAYOUT
# ==============================================================================

# --- Sidebar ---
st.sidebar.title("⚙️ Configuration")
st.sidebar.success("System Connected")

# Load Categories dynamically
category_options = ["General"]
if sh:
    try:
        ws_cat = sh.worksheet("Category_Rules")
        headers = ws_cat.row_values(1)
        if headers:
            category_options = headers
    except:
        st.sidebar.warning("Could not load categories from 'Category_Rules' tab.")

# --- Main Area ---
st.title("🛡️ BuyClub Page Analyzer")
st.markdown("---")

col1, col2 = st.columns(2)

with col1:
    deal_name = st.text_input("Deal Name (Required for Archive)", placeholder="e.g. Burger King Geneva")
    page_url = st.text_input("Current Page URL (Required)", placeholder="https://buyclub.ch/...")
    category = st.selectbox("Category", category_options)

with col2:
    prev_url = st.text_input("Previous Deal URL (Optional)", placeholder="https://buyclub.ch/...")
    contract_file = st.file_uploader("Contract / Sale Conditions", type=['pdf', 'txt'])
    specific_instructions = st.text_area("Specific Instructions", height=100, placeholder="e.g., Check expiration date carefully.")

analyze_btn = st.button("Analyze Page", type="primary", use_container_width=True)

# ==============================================================================
# MAIN LOGIC
# ==============================================================================

if analyze_btn:
    if not deal_name or not page_url:
        st.error("Deal Name and Page URL are mandatory.")
    else:
        with st.status("Running Compliance Analysis...", expanded=True) as status:
            
            # 1. Fetch Rules
            status.write("🧠 Accessing Hive Mind (Google Sheets)...")
            gen_rules, cat_rules, feed_log = get_rules(sh, category)
            
            # 2. Scrape Content
            status.write("🕷️ Scraping Web Content...")
            scraped_text = scrape_url(page_url)
            prev_text = scrape_url(prev_url) if prev_url else "N/A"
            
            # 3. Process Contract
            status.write("📄 Processing Contract...")
            contract_text = extract_text_from_file(contract_file)
            
            # 4. External Research
            status.write("🕵️‍♂️ Conducting External Research (Tavily)...")
            search_query = f"{deal_name} {category} reviews official website"
            search_results = perform_research(search_query)
            
            # 5. Gemini Analysis
            status.write("🤖 Generating Compliance Report (Gemini 1.5 Flash)...")
            report = analyze_with_gemini(
                scraped_text, prev_text, contract_text, search_results, 
                gen_rules, cat_rules, feed_log, specific_instructions
            )
            
            # 6. Archive
            status.write("💾 Archiving Results...")
            if sh:
                archive_report(sh, deal_name, category, report)
            
            status.update(label="Analysis Complete", state="complete", expanded=False)

        # Output Display
        st.markdown("### 📋 Compliance Report")
        st.markdown(report)

# ==============================================================================
# FEEDBACK LOOP
# ==============================================================================

st.markdown("---")
with st.expander("🧠 Teach the App (Add to Feedback Log)"):
    new_rule = st.text_input("Describe the error the AI missed or a new rule:")
    if st.button("Save Rule"):
        if new_rule and sh:
            save_feedback_rule(sh, new_rule)
        elif not sh:
            st.error("Database not connected.")
