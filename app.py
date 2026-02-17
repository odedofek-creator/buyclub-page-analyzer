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
import re

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
try:
    genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
    tavily = TavilyClient(api_key=st.secrets["TAVILY_API_KEY"])
except Exception as e:
    st.error(f"API Configuration Failed: {e}")
    st.stop()

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

def clean_search_term(deal_name):
    """
    Strips dates from the Deal Name so we search for the Business Name only.
    Example: "Amore Amore Feb 2026" -> "Amore Amore"
    """
    # Regex to find a space followed by a month name (English/French abbreviations)
    # Matches: space + (Jan|Feb...|Janv|Fev...) + anything after
    pattern = r"(?i)\s+\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|janv|fév|mars|avr|mai|juin|juil|août|sept|oct|nov|déc).*"
    
    clean_name = re.sub(pattern, "", deal_name).strip()
    return clean_name

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

def perform_research(deal_name, category):
    """
    Context-Aware Research (Precision Mode):
    1. Hotels -> Checks Booking.com + Google
    2. Restaurants -> Checks SPECIFIC Swiss Michelin/GM URLs + Google
    3. General -> Checks Google + Official Site
    4. BANS -> Wanderlog, Sluurpy, RestaurantGuru, Top10
    """
    try:
        # --- 1. CONFIGURATION ---
        banned_domains = [
            "wanderlog.com", 
            "restaurantguru.com", 
            "sluurpy.com",
            "top10.com",
            "trip.com"
        ]
        
        # --- 2. BUILD QUERIES BASED ON CATEGORY ---
        # Base search (Universal)
        queries = [f"{deal_name} {category} geneva google reviews official website"]
        
        # Category Specific Add-ons
        if category and "Restaurant" in category:
            # FORCE search inside the specific Swiss directories
            queries.append(f"site:guide.michelin.com/en/ch {deal_name}")
            queries.append(f"site:gaultmillau.ch {deal_name}")
        
        elif category and "Hotel" in category:
            # FORCE search inside Booking.com
            queries.append(f"site:booking.com {deal_name} geneva reviews")

        # --- 3. EXECUTE SEARCHES ---
        all_results = []
        for q in queries:
            # We use 'advanced' depth to get better snippets
            try:
                response = tavily.search(query=q, search_depth="advanced", max_results=5)
                all_results.extend(response.get('results', []))
            except:
                continue # Skip if one query fails
        
        # --- 4. FILTER & FORMAT RESULTS ---
        context_data = []
        seen_urls = set()

        for result in all_results:
            url = result['url']
            title = result['title']
            content = result['content']
            
            # Extract domain for checking
            domain = url.split('/')[2] if '//' in url else url.split('/')[0]
            
            # A. DUPLICATE CHECK
            if url in seen_urls:
                continue
            seen_urls.add(url)
            
            # B. BLACKLIST CHECK
            if any(bad in domain for bad in banned_domains):
                continue
            
            # C. SOURCE LABELLING
            source_label = "General Web"
            
            if "google" in domain:
                source_label = "GOOGLE REVIEWS (High Trust)"
            elif "booking.com" in domain:
                source_label = "BOOKING.COM (High Trust)"
            elif "michelin" in domain:
                source_label = "MICHELIN GUIDE SWITZERLAND (Authoritative)"
            elif "gaultmillau" in domain:
                source_label = "GAULT MILLAU (Authoritative)"
            elif "tripadvisor" in domain:
                source_label = "TRIPADVISOR"
            elif "facebook" in domain:
                source_label = "FACEBOOK"

            # D. DATA PACKAGING
            context_data.append(f"""
            SOURCE: {source_label}
            URL: {url}
            TITLE: {title}
            SNIPPET: {content}
            --------------------------------------------------
            """)
            
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

    try:
        model = genai.GenerativeModel(
            model_name='gemini-2.5-flash',
            system_instruction=system_prompt
        )
        response = model.generate_content(user_prompt)
        return response.text
    except Exception as e:
        return f"FATAL ERROR: {str(e)}"

def archive_report(sheet_obj, deal_name, category, report_text):
    """Parses score and saves full report to Archive tab."""
    try:
        ws = sheet_obj.worksheet("Analysis_Archive")
        
        # Extract score roughly (looking for "Score: XX" or similar)
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

# NEW UI LAYOUT
# Row 1: The Basics
col_a1, col_a2 = st.columns([2, 1])
with col_a1:
    deal_name = st.text_input("Deal Name (e.g. 'Amore Amore Feb 2026')", placeholder="Business Name + Date")
with col_a2:
    category = st.selectbox("Category", category_options)

# Row 2: The Links
col_b1, col_b2 = st.columns(2)
with col_b1:
    page_url = st.text_input("Current Page URL (Required)", placeholder="https://buyclub.ch/...")
with col_b2:
    prev_url = st.text_input("Previous Deal URL (Optional)", placeholder="https://buyclub.ch/...")

# Row 3: Documents & Context
col_c1, col_c2 = st.columns(2)
with col_c1:
    contract_file = st.file_uploader("Contract / Sale Conditions", type=['pdf', 'txt'])
with col_c2:
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
            
            # 4. External Research (WITH SMART CLEANER)
            # A. Clean the name
            search_name = clean_search_term(deal_name)
            status.write(f"🕵️‍♂️ Conducting Deep Research for '{search_name}'...")
            
            # B. Run Research
            search_results = perform_research(search_name, category)
            
            # 5. Gemini Analysis
            status.write("🤖 Generating Compliance Report (Gemini 2.5 Flash)...")
            report = analyze_with_gemini(
                scraped_text, prev_text, contract_text, search_results, 
                gen_rules, cat_rules, feed_log, specific_instructions
            )
            
            # 6. Archive
            status.write("💾 Archiving Results...")
            if sh and report and "FATAL ERROR" not in report:
                archive_report(sh, deal_name, category, report)
            
            status.update(label="Analysis Complete", state="complete", expanded=False)

        # Output Display
        st.markdown("### 📋 Compliance Report")
        if report and "FATAL ERROR" in report:
            st.error(report)
        else:
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
