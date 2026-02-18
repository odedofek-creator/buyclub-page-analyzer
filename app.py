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
import time

# ==============================================================================
# CONFIGURATION & SETUP
# ==============================================================================

st.set_page_config(page_title="BuyClub Page Analyzer", layout="wide", page_icon="🛡️")

# Debug mode (set DEBUG_MODE=true in secrets to enable)
DEBUG_MODE = st.secrets.get("DEBUG_MODE", "false").lower() == "true"

# --- PASSWORD PROTECTION START ---
def check_password():
    """Returns `True` if the user had the correct password."""

    # 1. Initialize State
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    # 2. If already logged in, return True immediately
    if st.session_state.password_correct:
        return True

    # 3. Show the Login Form
    with st.form("login_form"):
        st.header("🔒 Login Required")
        password_input = st.text_input("Enter Password", type="password")
        submit_button = st.form_submit_button("Login", type="primary")

    # 4. Check Password ONLY when button is clicked
    if submit_button:
        if password_input == st.secrets["APP_PASSWORD"]:
            st.session_state.password_correct = True
            st.rerun()  # Force reload to show app
        else:
            st.error("😕 Password incorrect")
            time.sleep(1)  # Security delay

    # 5. If we reach here, user is not logged in yet
    return False

if not check_password():
    st.stop()
# --- PASSWORD PROTECTION END ---

# Verify Secrets
required_secrets = ["GOOGLE_API_KEY", "TAVILY_API_KEY", "gcp_service_account", "APP_PASSWORD"]
if not all(k in st.secrets for k in required_secrets):
    st.error("🚨 Missing API Keys in .streamlit/secrets.toml")
    st.stop()

# Initialize APIs (IMPROVEMENT #1: Better error handling - no key leakage)
try:
    genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
    tavily = TavilyClient(api_key=st.secrets["TAVILY_API_KEY"])
except Exception as e:
    st.error("🚨 API Configuration Failed. Please check your secrets configuration.")
    if DEBUG_MODE:
        st.exception(e)
    st.stop()

# Google Sheets Connector
@st.cache_resource
def init_google_sheets():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(dict(st.secrets["gcp_service_account"]), scope)
        client = gspread.authorize(creds)
        sheet = client.open("BuyClub_Page_Analyzer_Brain")
        return sheet
    except Exception as e:
        st.error("Failed to connect to Google Sheets. Please verify your service account credentials.")
        if DEBUG_MODE:
            st.exception(e)
        return None

sh = init_google_sheets()

# ==============================================================================
# SESSION STATE INITIALIZATION
# ==============================================================================
if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None
if 'current_archive_name' not in st.session_state:
    st.session_state.current_archive_name = ""
if 'current_category' not in st.session_state:
    st.session_state.current_category = ""
# IMPROVEMENT #5: Rate limiting
if 'last_analysis_time' not in st.session_state:
    st.session_state.last_analysis_time = 0

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def scrape_url(url):
    """Basic text extraction from a URL."""
    # IMPROVEMENT #3: URL validation
    if not url or not url.startswith(('http://', 'https://')):
        return "Error scraping URL: Invalid URL format (must start with http:// or https://)"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        # IMPROVEMENT #7: Longer timeout for slow pages
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        for script in soup(["script", "style", "nav", "footer"]):
            script.extract()
            
        text = soup.get_text(separator="\n")
        cleaned_text = "\n".join([line.strip() for line in text.splitlines() if line.strip()])
        
        if DEBUG_MODE:
            st.info(f"✅ Scraped {len(cleaned_text)} characters from {url}")
        
        return cleaned_text
    except Exception as e:
        return f"Error scraping URL: {str(e)}"

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
        
        if DEBUG_MODE and text:
            st.info(f"✅ Extracted {len(text)} characters from uploaded file")
            
    except Exception as e:
        text = f"Error reading file: {e}"
    return text

# IMPROVEMENT #2: Cache rules for 5 minutes
@st.cache_data(ttl=300)
def get_rules(sheet_name, category):
    """Fetches General Rules, Specific Category Rules, and Feedback."""
    # Re-initialize sheet connection inside cached function
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(dict(st.secrets["gcp_service_account"]), scope)
        client = gspread.authorize(creds)
        sheet_obj = client.open(sheet_name)
    except:
        return "", "", ""
    
    try:
        ws_gen = sheet_obj.worksheet("General_Rules")
        gen_rules = "\n".join([r[0] for r in ws_gen.get_all_values() if r])

        ws_cat = sheet_obj.worksheet("Category_Rules")
        cat_data = ws_cat.get_all_values()
        headers = cat_data[0]
        
        cat_rules_text = ""
        if category in headers:
            col_index = headers.index(category)
            rules = [row[col_index] for row in cat_data[1:] if len(row) > col_index and row[col_index].strip()]
            cat_rules_text = "\n".join(rules)
        else:
            cat_rules_text = "No specific rules found for this category."

        ws_feed = sheet_obj.worksheet("Feedback_Log")
        feed_rules = "\n".join([r[0] for r in ws_feed.get_all_values() if r])

        if DEBUG_MODE:
            st.info(f"✅ Loaded {len(gen_rules)} chars of general rules, {len(cat_rules_text)} chars of category rules")

        return gen_rules, cat_rules_text, feed_rules

    except Exception as e:
        st.error(f"Error fetching rules: {e}")
        return "", "", ""

def perform_research(merchant_name, category, location="Geneva", treatment_terms=""):
    """
    Context-Aware Research.
    Uses 'merchant_name' + 'location' for searching.
    """
    try:
        banned_domains = ["wanderlog.com", "restaurantguru.com", "sluurpy.com", "top10.com", "trip.com"]
        
        # 1. Base Search (Universal) - Uses dynamic location
        queries = [f"{merchant_name} {location} google reviews official website"]
        
        # 2. Category Specific Searches
        if category and "Restaurant" in category:
            # A. Check French-Swiss Michelin & Gault Millau
            queries.append(f"site:guide.michelin.com/ch/fr {merchant_name}")
            queries.append(f"site:gaultmillau.ch/fr {merchant_name}")
            
            # B. Trusted Swiss News (Excluding Blogs)
            queries.append(f"site:lematin.ch OR site:20min.ch OR site:tdg.ch OR site:letemps.ch {merchant_name}")
            
        elif category and "Hotel" in category:
            # HOTELS
            queries.append(f"site:booking.com {merchant_name} {location} reviews")
            queries.append(f"site:tripadvisor.com {merchant_name} \"Certificate of Excellence\"")

        elif category and "Spa" in category:
            # SPAS: Check Magazines for TREATMENT
            search_scope = f"site:elle.com OR site:cosmopolitan.com OR site:vogue.com OR site:marieclaire.com"
            
            if treatment_terms:
                # If user entered "Microneedling, Botox" -> construct ("Microneedling" OR "Botox")
                terms = [t.strip() for t in treatment_terms.split(',')]
                joined_terms = " OR ".join(f'"{t}"' for t in terms)
                queries.append(f"{search_scope} ({joined_terms})")
            else:
                # Fallback
                queries.append(f"{search_scope} {merchant_name}")

        # 3. Execute Searches
        all_results = []
        for q in queries:
            try:
                response = tavily.search(query=q, search_depth="advanced", max_results=5)
                all_results.extend(response.get('results', []))
            except:
                continue
        
        # 4. Filter & Format
        context_data = []
        seen_urls = set()

        for result in all_results:
            url = result['url']
            title = result['title']
            content = result['content']
            
            domain = url.split('/')[2] if '//' in url else url.split('/')[0]
            
            if url in seen_urls: continue
            seen_urls.add(url)
            
            if any(bad in domain for bad in banned_domains): continue
            
            source_label = "General Web"
            if "google" in domain: source_label = "GOOGLE REVIEWS"
            elif "booking.com" in domain: source_label = "BOOKING.COM"
            elif "michelin" in domain: source_label = "MICHELIN GUIDE (Swiss/FR)"
            elif "gaultmillau" in domain: source_label = "GAULT MILLAU (Swiss/FR)"
            elif "tripadvisor" in domain: source_label = "TRIPADVISOR"
            elif "lematin" in domain or "20min" in domain or "tdg.ch" in domain: source_label = "SWISS PRESS"
            elif "elle" in domain or "vogue" in domain or "cosmo" in domain: source_label = "FASHION MAGAZINE"

            context_data.append(f"SOURCE: {source_label}\nURL: {url}\nTITLE: {title}\nSNIPPET: {content}\n-------------------")
        
        if DEBUG_MODE:
            st.info(f"✅ Found {len(context_data)} unique research results from {len(queries)} queries")
            
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
    - No pleasantries. Start immediately with the data.
    - Zero Hallucinations.
    
    ANALYSIS STRUCTURE:
    1. 📊 Executive Summary (Score 0-100 & Verdict)
    2. 🚨 Section 1: Critical Issues (Contract Mismatches, Regression, Factual Errors)
       - If no Contract is provided: Note it as a Warning (not a failure).
    3. ⚠️ Section 2: Compliance & Quality (Rules Broken, Spelling)
    4. 💡 Section 3: Marketing Opportunities (Awards Missing, Copy Improvements)
       - If you see a 'Certificate of Excellence' from TripAdvisor, flag it.
       - If you see a mention in Swiss Press (Le Matin, 20min), quote it.
       - If you see a mention in Elle, Cosmo, or Vogue, quote it.
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
        model = genai.GenerativeModel(model_name='gemini-2.5-flash', system_instruction=system_prompt)
        response = model.generate_content(user_prompt)
        return response.text
    except Exception as e:
        # IMPROVEMENT #10: Better Gemini error handling
        error_str = str(e).lower()
        if "quota" in error_str or "rate" in error_str or "resource" in error_str:
            return "FATAL ERROR: API rate limit exceeded. Please try again in a few minutes."
        elif "safety" in error_str or "blocked" in error_str:
            return "FATAL ERROR: Content was flagged by safety filters. Try rephrasing your input or contact support."
        elif "invalid" in error_str and "api" in error_str:
            return "FATAL ERROR: API key issue. Please verify your GOOGLE_API_KEY in secrets."
        else:
            return f"FATAL ERROR: {str(e)}"

def archive_report(sheet_obj, deal_name, category, report_text):
    """Parses score and saves full report to Archive tab."""
    try:
        ws = sheet_obj.worksheet("Analysis_Archive")
        # IMPROVEMENT #4: More robust score regex
        score_match = re.search(r"Score[:\s]+(\d{1,3})(?:\D|$)", report_text, re.IGNORECASE)
        score = score_match.group(1) if score_match else "N/A"
        
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            deal_name,
            category,
            score,
            report_text
        ])
        # IMPROVEMENT #9: Better feedback on what was saved
        st.toast(f"✅ '{deal_name}' (Score: {score}) archived successfully!", icon="💾")
    except Exception as e:
        st.error(f"Archiving failed: {e}")

def save_feedback_rule(sheet_obj, rule_text):
    try:
        ws = sheet_obj.worksheet("Feedback_Log")
        ws.append_row([rule_text, datetime.now().strftime("%Y-%m-%d")])
        st.success("✅ Rule learned and saved to Feedback Log.")
    except Exception as e:
        st.error(f"Failed to save rule: {e}")

# ==============================================================================
# UI LAYOUT
# ==============================================================================

# Row 1: The Basics
col_a1, col_a2, col_a3 = st.columns([1.5, 1.5, 1])
with col_a1:
    archive_name = st.text_input("Deal Name (For Archive)", placeholder="e.g. Amore Amore Feb 2026")
with col_a2:
    merchant_name = st.text_input("Merchant / Venue Name (For Search)", placeholder="e.g. Amore Amore")
with col_a3:
    # Safe category loading that won't crash if sheets unavailable
    category_options = ["General"]
    if sh:
        try:
            cat_headers = sh.worksheet("Category_Rules").row_values(1)
            if cat_headers:
                category_options = cat_headers
        except:
            pass
    category = st.selectbox("Category", category_options)

# Row 2: Location & URL
col_b1, col_b2 = st.columns([1, 2])
with col_b1:
    location = st.text_input("City / Location", value="Geneva")
with col_b2:
    page_url = st.text_input("Current Page URL (Required)", placeholder="https://buyclub.ch/...")

# Row 3: Previous Deal & Documents
col_c1, col_c2 = st.columns(2)
with col_c1:
    prev_url = st.text_input("Previous Deal URL (Optional)", placeholder="https://buyclub.ch/...")
    contract_file = st.file_uploader("Contract / Sale Conditions", type=['pdf', 'txt'])
with col_c2:
    # IMPROVEMENT #8: Make search keywords generic, only show treatment field for Spas
    if category and "Spa" in category:
        treatment_term = st.text_input("Treatment(s) - For Magazine Search", placeholder="e.g. Microneedling, Botox")
    else:
        treatment_term = ""
    specific_instructions = st.text_area("Specific Instructions (Logic)", height=70)

analyze_btn = st.button("Analyze Page", type="primary", use_container_width=True)

# ==============================================================================
# MAIN LOGIC
# ==============================================================================

if analyze_btn:
    if not archive_name or not merchant_name or not page_url:
        st.error("Archive Name, Merchant Name, and Page URL are mandatory.")
    else:
        # IMPROVEMENT #5: Rate limiting (10 second cooldown)
        time_since_last = time.time() - st.session_state.last_analysis_time
        if time_since_last < 10:
            st.warning(f"⏳ Please wait {int(10 - time_since_last)} seconds before analyzing again.")
        else:
            st.session_state.last_analysis_time = time.time()
            
            # Clear old results at START of new analysis
            st.session_state.analysis_result = None
            st.session_state.current_archive_name = ""
            st.session_state.current_category = ""
            
            with st.status("Running Compliance Analysis...", expanded=True) as status:
                status.write("🧠 Accessing Hive Mind...")
                gen_rules, cat_rules, feed_log = get_rules("BuyClub_Page_Analyzer_Brain", category)
                
                status.write("🕷️ Scraping Content...")
                scraped_text = scrape_url(page_url)
                
                # Catch scraping errors early
                if scraped_text.startswith("Error scraping"):
                    st.error(f"Failed to scrape page: {scraped_text}")
                    status.update(label="❌ Scraping Failed", state="error", expanded=False)
                    st.stop()
                
                prev_text = scrape_url(prev_url) if prev_url else "N/A"
                contract_text = extract_text_from_file(contract_file)
                
                # Use explicit Merchant Name + Location for search
                status.write(f"🕵️‍♂️ Researching '{merchant_name}' in {location}...")
                search_results = perform_research(merchant_name, category, location, treatment_term)
                
                status.write("🤖 Analyzing...")
                # Add loading indicator for Gemini
                with st.spinner("Waiting for Gemini response..."):
                    report = analyze_with_gemini(
                        scraped_text, prev_text, contract_text, search_results, 
                        gen_rules, cat_rules, feed_log, specific_instructions
                    )
                
                # Save to session state
                st.session_state.analysis_result = report
                st.session_state.current_archive_name = archive_name
                st.session_state.current_category = category
                
                status.update(label="✅ Analysis Complete", state="complete", expanded=False)

# ==============================================================================
# DISPLAY REPORT & ACTIONS
# ==============================================================================

if st.session_state.analysis_result:
    
    # --- ACTION BUTTONS (ON TOP) ---
    col_act1, col_act2 = st.columns(2)
    
    with col_act1:
        if st.button("💾 Save to Archive", use_container_width=True):
            if sh:
                with st.spinner("Saving to Google Sheets..."):
                    archive_report(sh, st.session_state.current_archive_name, st.session_state.current_category, st.session_state.analysis_result)
            else:
                st.error("Cannot save: Google Sheets connection unavailable")
    
    with col_act2:
        if st.button("🗑️ Trash / Clear", use_container_width=True):
            st.session_state.analysis_result = None
            st.session_state.current_archive_name = ""
            st.session_state.current_category = ""

    # Check for None before displaying (prevents crash when trash button clicked)
    if st.session_state.analysis_result:
        st.markdown("---")
        
        st.markdown("### 📋 Compliance Report")
        
        if "FATAL ERROR" in st.session_state.analysis_result:
            st.error(st.session_state.analysis_result)
        else:
            st.markdown(st.session_state.analysis_result)

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

# Debug info footer
if DEBUG_MODE:
    st.markdown("---")
    st.caption("🔧 Debug Mode Active | Check info boxes above for detailed logs")
