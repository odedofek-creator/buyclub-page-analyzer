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
from PIL import Image

# ==============================================================================
# CONFIGURATION & SETUP
# ==============================================================================

st.set_page_config(page_title="BuyClub Page Analyzer", layout="wide", page_icon="🛡️")

# Debug mode (set DEBUG_MODE=true in secrets to enable)
DEBUG_MODE = st.secrets.get("DEBUG_MODE", "false").lower() == "true"

# --- PASSWORD PROTECTION START ---
def check_password():
    """Returns `True` if the user had the correct password."""

    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    if st.session_state.password_correct:
        return True

    with st.form("login_form"):
        st.header("🔒 Login Required")
        password_input = st.text_input("Enter Password", type="password")
        submit_button = st.form_submit_button("Login", type="primary")

    if submit_button:
        if password_input == st.secrets["APP_PASSWORD"]:
            st.session_state.password_correct = True
            st.rerun() 
        else:
            st.error("😕 Password incorrect")
            time.sleep(1)

    return False

if not check_password():
    st.stop()
# --- PASSWORD PROTECTION END ---

# Verify Secrets
required_secrets = ["GOOGLE_API_KEY", "TAVILY_API_KEY", "gcp_service_account", "APP_PASSWORD"]
if not all(k in st.secrets for k in required_secrets):
    st.error("🚨 Missing API Keys in .streamlit/secrets.toml")
    st.stop()

# Initialize APIs
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
if 'last_analysis_time' not in st.session_state:
    st.session_state.last_analysis_time = 0

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def scrape_url(url):
    """Basic text extraction from a URL."""
    if not url or not url.startswith(('http://', 'https://')):
        return "Error scraping URL: Invalid URL format (must start with http:// or https://)"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
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
    """Extracts text from PDF, TXT, or Image (JPEG/PNG)."""
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
            
        elif uploaded_file.type in ["image/jpeg", "image/png", "image/jpg"]:
            image = Image.open(uploaded_file)
            ocr_model = genai.GenerativeModel('gemini-3.5-flash')
            ocr_response = ocr_model.generate_content([
                "Extract all the text from this contract/document exactly as it appears. Do not add any extra commentary.", 
                image
            ])
            text = ocr_response.text
            if DEBUG_MODE:
                st.info(f"👁️ Gemini Vision extracted text from image.")
                
        else:
            text = "[Unsupported File Uploaded]"
        
        if DEBUG_MODE and text:
            st.info(f"✅ Extracted {len(text)} characters from uploaded file")
            
    except Exception as e:
        text = f"Error reading file: {e}"
    return text

@st.cache_data(ttl=300)
def get_rules(sheet_name, category):
    """Fetches General Rules, Specific Category Rules, and Feedback."""
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
    """
    try:
        banned_domains = ["wanderlog.com", "restaurantguru.com", "sluurpy.com", "top10.com", "trip.com"]
        queries = [f"{merchant_name} {location} google reviews official website"]
        
        if category and "Restaurant" in category:
            queries.append(f"site:guide.michelin.com/ch/fr {merchant_name}")
            queries.append(f"site:gaultmillau.ch/fr {merchant_name}")
            queries.append(f"site:lematin.ch OR site:20min.ch OR site:tdg.ch OR site:letemps.ch {merchant_name}")
            
        elif category and "Hotel" in category:
            queries.append(f"site:booking.com {merchant_name} {location} reviews")
            queries.append(f"site:tripadvisor.com {merchant_name} \"Certificate of Excellence\"")

        elif category and "Spa" in category:
            search_scope = f"site:elle.com OR site:cosmopolitan.com OR site:vogue.com OR site:marieclaire.com"
            if treatment_terms:
                # ONLY search for treatments in the magazines (ignoring merchant name for this specific query)
                terms = [t.strip() for t in treatment_terms.split(',')]
                joined_terms = " OR ".join(f'"{t}"' for t in terms)
                queries.append(f"{search_scope} ({joined_terms})")
            else:
                queries.append(f"{search_scope} {merchant_name}")

        all_results = []
        for q in queries:
            try:
                response = tavily.search(query=q, search_depth="advanced", max_results=5)
                all_results.extend(response.get('results', []))
            except:
                continue
        
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
    
    CRITICAL RULE FOR CONTRACTS:
    If you receive both [PASTED TEXT] and an [UPLOADED FILE], you must use both sources to understand the deal. HOWEVER, if there is ANY contradiction between the two sources, the [PASTED TEXT] is the absolute truth and strictly overrides the [UPLOADED FILE].
    
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
    
    [CONTRACT TEXT]: 
    {contract_txt}
    
    [PREVIOUS DEAL TEXT]: {prev_txt}
    [CURRENT PAGE TEXT (TARGET)]: {scraped_txt}
    [EXTERNAL SEARCH RESEARCH]: {search_data}
    """

    try:
        model = genai.GenerativeModel(model_name='gemini-3.5-flash', system_instruction=system_prompt)
        response = model.generate_content(user_prompt)
        return response.text
    except Exception as e:
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
        score_match = re.search(r"Score[:\s]+(\d{1,3})(?:\D|$)", report_text, re.IGNORECASE)
        score = score_match.group(1) if score_match else "N/A"
        
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            deal_name,
            category,
            score,
            report_text
        ])
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

tab1, tab2, tab3 = st.tabs(["🔍 Marketing Researcher", "🛡️ Page Analyzer", "📋 Archive Viewer"])

# ==============================================================================
# TAB 1 — MARKETING RESEARCHER
# ==============================================================================

with tab1:

    # --------------------------------------------------------------------------
    # SESSION STATE
    # --------------------------------------------------------------------------
    if 'research_result' not in st.session_state:
        st.session_state.research_result = None
    if 'research_raw_data' not in st.session_state:
        st.session_state.research_raw_data = None
    if 'research_archive_name' not in st.session_state:
        st.session_state.research_archive_name = ""
    if 'research_category' not in st.session_state:
        st.session_state.research_category = ""
    if 'research_venue_name' not in st.session_state:
        st.session_state.research_venue_name = ""
    if 'research_city' not in st.session_state:
        st.session_state.research_city = ""
    if 'research_country' not in st.session_state:
        st.session_state.research_country = ""

    # --------------------------------------------------------------------------
    # HELPER FUNCTIONS
    # --------------------------------------------------------------------------

    def crawl_venue_url(url):
        """Crawl venue website via Tavily and return raw content."""
        try:
            response = tavily.extract(urls=[url])
            if response and response.get('results'):
                return response['results'][0].get('raw_content', '')
            return ""
        except Exception:
            return ""

    def extract_name_and_city_from_crawl(crawl_content):
        """Use Gemini to extract venue name and city from crawled website content."""
        try:
            model = genai.GenerativeModel(model_name='gemini-3.5-flash')
            prompt = f"""From the following website content, extract:
1. The venue/business name
2. The city it is located in

Reply in this exact format (two lines only):
NAME: <venue name>
CITY: <city name>

If you cannot determine one of them, write NOT FOUND for that field.

WEBSITE CONTENT:
{crawl_content[:3000]}"""
            response = model.generate_content(prompt)
            text = response.text.strip()
            name, city = "NOT FOUND", "NOT FOUND"
            for line in text.splitlines():
                if line.startswith("NAME:"):
                    name = line.replace("NAME:", "").strip()
                elif line.startswith("CITY:"):
                    city = line.replace("CITY:", "").strip()
            return name, city
        except Exception:
            return "NOT FOUND", "NOT FOUND"

    def perform_researcher_research(venue_name, category, city, country, treatment_terms="", venue_url=""):
        """Run Tavily searches for the Marketing Researcher tab."""
        try:
            banned_domains = ["wanderlog.com", "restaurantguru.com", "sluurpy.com", "top10.com", "trip.com"]
            location_str = f"{city}, {country}" if country != "Switzerland" else city

            queries = [
                f"{venue_name} {city} google reviews",
                f"{venue_name} {city}",
            ]

            # Venue website crawl
            if venue_url:
                queries.append(venue_url)

            if "Restaurant" in category:
                if country == "France":
                    queries.append(f"site:guide.michelin.com/fr {venue_name}")
                    queries.append(f"site:gaultmillau.fr {venue_name}")
                    queries.append(f"site:lefigaro.fr OR site:lemonde.fr OR site:20minutes.fr {venue_name}")
                else:
                    queries.append(f"site:guide.michelin.com/ch/fr {venue_name}")
                    queries.append(f"site:gaultmillau.ch {venue_name}")
                    queries.append(f"site:lematin.ch OR site:20min.ch OR site:tdg.ch OR site:letemps.ch {venue_name}")
                queries.append(f"site:tripadvisor.com {venue_name} {city} \"Certificate of Excellence\"")

            elif "Simple Beauty Treatment" in category:
                queries.append(f"{venue_name} {city} press review")
                if treatment_terms:
                    terms = [t.strip() for t in treatment_terms.split(',')]
                    for term in terms:
                        queries.append(f"{term} treatment benefits")

            elif "High-Tech Aesthetic Treatment" in category:
                queries.append(f"{venue_name} {city} press review")
                if treatment_terms:
                    terms = [t.strip() for t in treatment_terms.split(',')]
                    search_scope = "site:elle.com OR site:cosmopolitan.com OR site:vogue.com OR site:marieclaire.com"
                    joined_terms = " OR ".join(f'"{t}"' for t in terms)
                    queries.append(f"{search_scope} ({joined_terms})")
                    for term in terms:
                        queries.append(f"{term} clinical study scientific evidence FDA")

            all_results = []
            for q in queries:
                try:
                    response = tavily.search(query=q, search_depth="advanced", max_results=5)
                    all_results.extend(response.get('results', []))
                except Exception:
                    continue

            context_data = []
            seen_urls = set()
            for result in all_results:
                url = result['url']
                title = result['title']
                content = result['content']
                domain = url.split('/')[2] if '//' in url else url.split('/')[0]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                if any(bad in domain for bad in banned_domains):
                    continue

                source_label = "General Web"
                if "google" in domain:
                    source_label = "GOOGLE REVIEWS"
                elif "michelin" in domain:
                    source_label = "MICHELIN GUIDE"
                elif "gaultmillau" in domain:
                    source_label = "GAULT MILLAU"
                elif "tripadvisor" in domain:
                    source_label = "TRIPADVISOR"
                elif any(d in domain for d in ["lematin", "20min", "tdg.ch", "letemps", "lefigaro", "lemonde", "20minutes.fr"]):
                    source_label = "PRESS"
                elif any(d in domain for d in ["elle.com", "vogue.com", "cosmopolitan.com", "marieclaire.com"]):
                    source_label = "BEAUTY PRESS"

                context_data.append(f"SOURCE: {source_label}\nURL: {url}\nTITLE: {title}\nSNIPPET: {content}\n-------------------")

            return "\n".join(context_data)
        except Exception as e:
            return f"Search failed: {e}"

    def run_researcher_gemini(venue_name, city, country, category, treatment_terms, special_instructions, research_data, gen_rules):
        """Run Gemini in Marketing Researcher persona and return structured brief."""

        if "Restaurant" in category:
            output_structure = """
OUTPUT STRUCTURE (use these exact section headers):
## Overview
## Ratings & Awards
## Press Mentions
## Key Marketing Points
## Not Found
"""
        elif "Simple Beauty Treatment" in category:
            output_structure = """
OUTPUT STRUCTURE (use these exact section headers):
## Treatment Overview
## About the Venue
## Venue Reviews & Ratings
## Press Mentions
## Key Marketing Points
## Not Found
"""
        elif "High-Tech Aesthetic Treatment" in category:
            output_structure = """
OUTPUT STRUCTURE (use these exact section headers):
## Treatment Overview
## Clinical & Scientific Backing
## Beauty Press Coverage
## About the Venue
## Venue Reviews & Ratings
## Key Marketing Points
## Not Found
"""
        else:
            output_structure = """
OUTPUT STRUCTURE (use these exact section headers):
## Overview
## Reviews & Ratings
## Key Marketing Points
## Not Found
"""

        system_prompt = f"""You are a Marketing Researcher for BuyClub, a premium members-only deals platform in Geneva and Lausanne.
Your job is to produce a sourced marketing brief about a venue or treatment to help a copywriter write a compelling deal page.

CITATION RULES — MANDATORY:
- Every factual claim about the venue must include a real clickable markdown link: [source name](full URL). No URL = do not include the claim.
- Ratings (Google, TripAdvisor, etc.) must only be cited if found directly on that platform's own domain (google.com, tripadvisor.com). If a rating is mentioned on a third-party site (e.g. Instagram, a blog, another clinic), label it clearly: "Reported as X stars (unverified — source: [name](url))".
- Clinical and scientific claims must come from peer-reviewed journals, government health agencies (FDA, WHO, ANSM), or official hospital/university publications. Claims from beauty clinic websites, influencer articles, or commercial sites do NOT count as scientific backing — omit them or move them to General Information.
- General Information (treatment descriptions, mechanisms, generic benefits from your training knowledge) does not need a URL but must be clearly labeled as "General Information".
- Ignore low-authority sources: personal blogs, forum posts, aggregators.

OUTPUT RULES:
- If a section has no findings, write "Not found." under that header — do not skip it.
- The "Not Found" section at the end must list each specific thing that was searched for but not found. For example: "Michelin Guide mention: not found.", "TripAdvisor Certificate of Excellence: not found.", "Swiss press coverage: not found." Do not just write "Not found." with no context.
- Be specific and useful. The copywriter needs real claims they can use on a deal page.
- Output in English only.

{output_structure}"""

        user_prompt = f"""
VENUE: {venue_name}
CITY: {city}
COUNTRY: {country}
CATEGORY: {category}
{"TREATMENT(S): " + treatment_terms if treatment_terms else ""}
{"SPECIAL INSTRUCTIONS: " + special_instructions if special_instructions else ""}

GENERAL RULES FROM SHEET:
{gen_rules}

RESEARCH DATA:
{research_data}
"""

        try:
            model = genai.GenerativeModel(model_name='gemini-3.5-flash', system_instruction=system_prompt)
            response = model.generate_content(user_prompt)
            return response.text
        except Exception as e:
            return f"FATAL ERROR: {str(e)}"

    def archive_research(sheet_obj, deal_name, category, venue_name, city, country, brief_text):
        """Save research brief to Research_Archive tab."""
        try:
            ws = sheet_obj.worksheet("Research_Archive")
            ws.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                deal_name,
                category,
                venue_name,
                city,
                country,
                brief_text
            ])
            st.toast(f"✅ '{deal_name}' research saved to archive.", icon="💾")
        except Exception as e:
            st.error(f"Archiving failed: {e}")

    # --------------------------------------------------------------------------
    # FORM
    # --------------------------------------------------------------------------

    r_col_cat, r_col_deal = st.columns([1, 2])
    with r_col_cat:
        r_category_options = ["General"]
        if sh:
            try:
                r_cat_headers = sh.worksheet("Category_Rules").row_values(1)
                if r_cat_headers:
                    r_category_options = r_cat_headers
            except Exception:
                pass
        r_category = st.selectbox("Category", r_category_options, key="r_category")
    with r_col_deal:
        r_deal_name = st.text_input("Deal Name (For Archive)", placeholder="e.g. Amore Amore June 2026", key="r_deal_name")

    r_venue_url = st.text_input("Venue Website URL (Optional)", placeholder="https://venue-website.com", key="r_venue_url")

    r_col3, r_col4, r_col5 = st.columns([2, 1, 1])
    with r_col3:
        r_venue_name = st.text_input("Merchant / Venue Name", placeholder="Auto-filled from URL, or enter manually", key="r_venue_name")
    with r_col4:
        r_country = st.selectbox("Country", ["Switzerland", "France", "Other"], key="r_country")
    with r_col5:
        r_city = st.text_input("City", value="Geneva", key="r_city")

    if r_category and ("Simple Beauty Treatment" in r_category or "High-Tech Aesthetic Treatment" in r_category):
        r_treatments = st.text_input("Treatment(s) — comma-separated", placeholder="e.g. Microneedling, PRP", key="r_treatments")
    else:
        r_treatments = ""

    r_special = st.text_area("Special Instructions (Optional)", height=80, key="r_special")

    research_btn = st.button("Run Research", type="primary", use_container_width=True)

    # --------------------------------------------------------------------------
    # MAIN LOGIC
    # --------------------------------------------------------------------------

    if research_btn:
        if not r_deal_name:
            st.error("Deal Name is required.")
        elif not r_venue_name and not r_venue_url:
            st.error("Provide either a Venue URL or a Venue Name.")
        else:
            st.session_state.research_result = None

            with st.status("Running Research...", expanded=True) as r_status:

                resolved_name = r_venue_name.strip()
                resolved_city = r_city.strip()

                # URL-first: crawl and extract name/city if URL provided
                if r_venue_url:
                    r_status.write("🌐 Crawling venue website...")
                    crawl_content = crawl_venue_url(r_venue_url)
                    if crawl_content:
                        extracted_name, extracted_city = extract_name_and_city_from_crawl(crawl_content)

                        # Normalize text for comparison: lowercase + remove accents
                        def normalize(text):
                            replacements = {"é":"e","è":"e","ê":"e","ë":"e","à":"a","â":"a","ä":"a","î":"i","ï":"i","ô":"o","ö":"o","ù":"u","û":"u","ü":"u","ç":"c"}
                            t = text.lower().strip()
                            for accented, plain in replacements.items():
                                t = t.replace(accented, plain)
                            return t

                        # Known city aliases (pairs that should not trigger a conflict)
                        city_aliases = [
                            {"geneva", "geneve", "genf"},
                            {"zurich", "zurich"},
                            {"bern", "berne"},
                            {"basel", "bale"},
                            {"lausanne"},
                        ]

                        def cities_match(a, b):
                            na, nb = normalize(a), normalize(b)
                            if na == nb:
                                return True
                            for alias_group in city_aliases:
                                if na in alias_group and nb in alias_group:
                                    return True
                            return False

                        # Conflict detection: flag if extracted values differ from manual input
                        if resolved_name and extracted_name != "NOT FOUND":
                            if normalize(extracted_name) != normalize(resolved_name):
                                st.warning(f"⚠️ **Name conflict:** URL crawl found **\"{extracted_name}\"** but you entered **\"{resolved_name}\"**. Proceeding with your manual entry — double-check that the URL is for the right venue.")
                        if r_city.strip() and extracted_city != "NOT FOUND":
                            if not cities_match(extracted_city, r_city.strip()):
                                st.warning(f"⚠️ **City conflict:** URL crawl found **\"{extracted_city}\"** but you entered **\"{r_city.strip()}\"**. Proceeding with your manual entry.")

                        # Auto-fill only if manual fields are empty
                        if not resolved_name and extracted_name != "NOT FOUND":
                            resolved_name = extracted_name
                        if not r_venue_name and extracted_city != "NOT FOUND":
                            resolved_city = extracted_city
                    else:
                        r_status.write("⚠️ Could not crawl venue URL — using manually entered details.")

                if not resolved_name:
                    st.error("Could not determine venue name from URL. Please enter it manually.")
                    r_status.update(label="❌ Missing venue name", state="error", expanded=False)
                    st.stop()

                r_status.write(f"🕵️ Researching '{resolved_name}' in {resolved_city}...")
                gen_rules_r, _, _ = get_rules("BuyClub_Page_Analyzer_Brain", r_category)
                research_data = perform_researcher_research(
                    resolved_name, r_category, resolved_city, r_country, r_treatments, r_venue_url
                )

                r_status.write("🤖 Building marketing brief...")
                brief = run_researcher_gemini(
                    resolved_name, resolved_city, r_country, r_category,
                    r_treatments, r_special, research_data, gen_rules_r
                )

                st.session_state.research_result = brief
                st.session_state.research_raw_data = research_data
                st.session_state.research_archive_name = r_deal_name
                st.session_state.research_category = r_category
                st.session_state.research_venue_name = resolved_name
                st.session_state.research_city = resolved_city
                st.session_state.research_country = r_country

                r_status.update(label="✅ Research Complete", state="complete", expanded=False)

    # --------------------------------------------------------------------------
    # DISPLAY BRIEF & ACTIONS
    # --------------------------------------------------------------------------

    if st.session_state.research_result:

        r_act1, r_act2 = st.columns(2)
        with r_act1:
            if st.button("💾 Save to Research Archive", use_container_width=True):
                if sh:
                    with st.spinner("Saving..."):
                        archive_research(
                            sh,
                            st.session_state.research_archive_name,
                            st.session_state.research_category,
                            st.session_state.research_venue_name,
                            st.session_state.research_city,
                            st.session_state.research_country,
                            st.session_state.research_result
                        )
                else:
                    st.error("Cannot save: Google Sheets connection unavailable.")
        with r_act2:
            if st.button("🗑️ Clear", use_container_width=True, key="r_clear"):
                st.session_state.research_result = None
                st.session_state.research_raw_data = None

        if st.session_state.research_result:
            st.markdown("---")
            st.markdown("### 📋 Marketing Brief")
            if "FATAL ERROR" in st.session_state.research_result:
                st.error(st.session_state.research_result)
            else:
                st.markdown(st.session_state.research_result)

    # --------------------------------------------------------------------------
    # FEEDBACK LOOP
    # --------------------------------------------------------------------------

    st.markdown("---")
    with st.expander("🧠 Teach the App (Add to Feedback Log)"):
        r_new_rule = st.text_input("Describe a rule or correction:", key="r_new_rule")
        if st.button("Save Rule", key="r_save_rule"):
            if r_new_rule and sh:
                save_feedback_rule(sh, r_new_rule)
            elif not sh:
                st.error("Database not connected.")

# ==============================================================================
# TAB 2 — PAGE ANALYZER
# ==============================================================================

with tab2:

    # Row 1: The Basics
    col_a1, col_a2, col_a3 = st.columns([1.5, 1.5, 1])
    with col_a1:
        archive_name = st.text_input("Deal Name (For Archive)", placeholder="e.g. Amore Amore Feb 2026")
    with col_a2:
        merchant_name = st.text_input("Merchant / Venue Name (For Search)", placeholder="e.g. Amore Amore")
    with col_a3:
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

        contract_file = st.file_uploader("Upload Contract File", type=['pdf', 'txt', 'png', 'jpg', 'jpeg'])
        contract_pasted = st.text_area("Or Paste Contract Text (Overrides File if Conflicts Exist)", height=68, placeholder="Paste contract text here...")

    with col_c2:
        if category and "Spa" in category:
            treatment_term = st.text_input("Treatment(s) - For Magazine Search", placeholder="e.g. Microneedling, Botox")
        else:
            treatment_term = ""
        specific_instructions = st.text_area("Specific Instructions (Logic)", height=155)

    analyze_btn = st.button("Analyze Page", type="primary", use_container_width=True)

    # --------------------------------------------------------------------------
    # MAIN LOGIC
    # --------------------------------------------------------------------------

    if analyze_btn:
        if not archive_name or not merchant_name or not page_url:
            st.error("Archive Name, Merchant Name, and Page URL are mandatory.")
        else:
            time_since_last = time.time() - st.session_state.last_analysis_time
            if time_since_last < 10:
                st.warning(f"⏳ Please wait {int(10 - time_since_last)} seconds before analyzing again.")
            else:
                st.session_state.last_analysis_time = time.time()

                st.session_state.analysis_result = None
                st.session_state.current_archive_name = ""
                st.session_state.current_category = ""

                with st.status("Running Compliance Analysis...", expanded=True) as status:
                    status.write("🧠 Accessing Hive Mind...")
                    gen_rules, cat_rules, feed_log = get_rules("BuyClub_Page_Analyzer_Brain", category)

                    status.write("🕷️ Scraping Content...")
                    scraped_text = scrape_url(page_url)

                    if scraped_text.startswith("Error scraping"):
                        st.error(f"Failed to scrape page: {scraped_text}")
                        status.update(label="❌ Scraping Failed", state="error", expanded=False)
                        st.stop()

                    prev_text = scrape_url(prev_url) if prev_url else "N/A"

                    status.write("📄 Processing Contract Data...")

                    # SMART CONTRACT EXTRACTION & COMBINATION
                    contract_text_from_file = extract_text_from_file(contract_file)
                    if contract_text_from_file.startswith("Error"):
                        contract_text_from_file = ""

                    contract_text = ""
                    if contract_pasted.strip() and contract_text_from_file:
                        contract_text = f"[PASTED TEXT (ABSOLUTE TRUTH - OVERRIDES UPLOADED FILE)]:\n{contract_pasted.strip()}\n\n[UPLOADED FILE]:\n{contract_text_from_file}"
                    elif contract_pasted.strip():
                        contract_text = f"[PASTED TEXT]:\n{contract_pasted.strip()}"
                    elif contract_text_from_file:
                        contract_text = f"[UPLOADED FILE]:\n{contract_text_from_file}"
                    else:
                        contract_text = "N/A"

                    # DYNAMIC SEARCH STATUS MESSAGE
                    if category and "Spa" in category and treatment_term:
                        status.write(f"🕵️‍♂️ Researching Venue '{merchant_name}' & Magazine Treatments '{treatment_term}'...")
                    else:
                        status.write(f"🕵️‍♂️ Researching '{merchant_name}' in {location}...")

                    search_results = perform_research(merchant_name, category, location, treatment_term)

                    status.write("🤖 Analyzing...")
                    with st.spinner("Waiting for Gemini response..."):
                        report = analyze_with_gemini(
                            scraped_text, prev_text, contract_text, search_results,
                            gen_rules, cat_rules, feed_log, specific_instructions
                        )

                    st.session_state.analysis_result = report
                    st.session_state.current_archive_name = archive_name
                    st.session_state.current_category = category

                    status.update(label="✅ Analysis Complete", state="complete", expanded=False)

    # --------------------------------------------------------------------------
    # DISPLAY REPORT & ACTIONS
    # --------------------------------------------------------------------------

    if st.session_state.analysis_result:

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

        if st.session_state.analysis_result:
            st.markdown("---")
            st.markdown("### 📋 Compliance Report")
            if "FATAL ERROR" in st.session_state.analysis_result:
                st.error(st.session_state.analysis_result)
            else:
                st.markdown(st.session_state.analysis_result)

    # --------------------------------------------------------------------------
    # FEEDBACK LOOP
    # --------------------------------------------------------------------------

    st.markdown("---")
    with st.expander("🧠 Teach the App (Add to Feedback Log)"):
        new_rule = st.text_input("Describe the error the AI missed or a new rule:")
        if st.button("Save Rule"):
            if new_rule and sh:
                save_feedback_rule(sh, new_rule)
            elif not sh:
                st.error("Database not connected.")

# ==============================================================================
# TAB 3 — ARCHIVE VIEWER (coming next)
# ==============================================================================

with tab3:
    st.info("📋 Archive Viewer — coming soon.")

if DEBUG_MODE:
    st.markdown("---")
    st.caption("🔧 Debug Mode Active | Check info boxes above for detailed logs")
