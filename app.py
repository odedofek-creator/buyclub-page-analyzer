import streamlit as st
import google.generativeai as genai

st.set_page_config(page_title="Model Diagnostic")

st.title("🕵️ Model Diagnostic Tool")

try:
    # Configure API
    genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
    
    st.write("### Attempting to list available models...")
    
    # List all models
    found_any = False
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            found_any = True
            st.success(f"✅ AVAILABLE: `{m.name}`")
            
    if not found_any:
        st.error("❌ Connection successful, but NO models were found. This usually means the API Key doesn't have access to the Generative Language API.")

except Exception as e:
    st.error(f"❌ CRITICAL ERROR: {e}")
    st.info("Check your 'requirements.txt' file. You might be using an old version of the library.")
