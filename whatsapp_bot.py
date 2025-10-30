import os
import json
import time
import re
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ===== ENVIRONMENT VARIABLES =====
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# Google Sheets Configuration
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")  # Service account credentials JSON
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "brookstone_verify_token_2024")
LEADS_SHEET_NAME = os.getenv("LEADS_SHEET_NAME", "Brookstone Leads")
SITE_VISITS_SHEET_NAME = os.getenv("SITE_VISITS_SHEET_NAME", "Brookstone Site Visits")
BROCHURE_MEDIA_ID = os.getenv("BROCHURE_MEDIA_ID", "1562506805130847")

# ===== LOAD FAQ DATA =====
def load_faq_data():
    """Load FAQ data from JSON files for both languages"""
    data = {}
    try:
        with open('faq_data_english.json', 'r', encoding='utf-8') as f:
            data['english'] = json.load(f)
    except Exception as e:
        logging.error(f"Error loading English FAQ: {e}")
        data['english'] = {}
    
    try:
        with open('faq_data_gujarati.json', 'r', encoding='utf-8') as f:
            data['gujarati'] = json.load(f)
    except Exception as e:
        logging.error(f"Error loading Gujarati FAQ: {e}")
        data['gujarati'] = {}
    
    return data

FAQ_DATA = load_faq_data()

# ===== IN-MEMORY CONVERSATION STATE =====
# For production, use Redis or a database
CONV_STATE = {}

# ===== LANGUAGE DETECTION =====
def detect_language(text):
    """Detect if text contains Gujarati characters"""
    # Gujarati Unicode range: U+0A80 to U+0AFF
    gujarati_chars = sum(1 for char in text if '\u0A80' <= char <= '\u0AFF')
    # If more than 20% of characters are Gujarati, consider it Gujarati
    if len(text) > 0 and gujarati_chars / len(text) > 0.2:
        return 'gujarati'
    return 'english'

# ===== WHATSAPP API FUNCTIONS =====
def send_whatsapp_text(to_phone, message):
    """Send a text message via WhatsApp Cloud API"""
    url = f"https://graph.facebook.com/v23.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message}
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            logging.info(f"✅ Message sent to {to_phone}")
            return True
        else:
            logging.error(f"❌ Failed to send message: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logging.error(f"❌ Error sending message: {e}")
        return False


def send_whatsapp_document(to_phone, document_id, caption="Here is your Brookstone Brochure 📄"):
    """Send WhatsApp document (PDF brochure) using Facebook Graph API"""
    url = f"https://graph.facebook.com/v23.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "document",
        "document": {
            "id": document_id,
            "caption": caption,
            "filename": "Brookstone.pdf"
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            logging.info(f"✅ Document sent to {to_phone}")
            return True
        else:
            logging.error(f"❌ Failed to send document: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logging.error(f"❌ Error sending document: {e}")
        return False


def mark_message_as_read(message_id):
    """Mark a WhatsApp message as read"""
    url = f"https://graph.facebook.com/v23.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id
    }
    
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Error marking message as read: {e}")


# ===== GOOGLE SHEETS FUNCTIONS =====
def get_google_creds():
    """Get Google credentials from environment variables"""
    try:
        # Get the credentials JSON from environment variable
        creds_json = os.getenv('GOOGLE_CREDENTIALS')
        if not creds_json:
            logging.error("GOOGLE_CREDENTIALS environment variable not set")
            return None
        
        # Parse the JSON string into a dictionary
        creds_dict = json.loads(creds_json)
        
        # Create credentials object
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        return creds
    except Exception as e:
        logging.error(f"Error creating Google credentials: {e}")
        return None

def check_new_bookings():
    """Check for new entries in the Google Sheet and send confirmation messages"""
    try:
        # Get credentials from environment
        creds = get_google_creds()
        if not creds:
            logging.error("Failed to get Google credentials")
            return False
        
        client = gspread.authorize(creds)
        
        # Open the site visits sheet
        sheet = client.open(SITE_VISITS_SHEET_NAME).sheet1
        
        # Get all records
        all_records = sheet.get_all_records()
        
        for record in all_records:
            # Check if this is a new record that hasn't been processed
            if not record.get('Status'):  # New form submissions won't have a status
                phone = record.get('Phone')
                name = record.get('Name')
                date = record.get('Preferred Date')
                time = record.get('Preferred Time')
                unit = record.get('Unit Type')
                budget = record.get('Budget')
                
                # Format phone number if needed
                if phone:
                    # Remove any spaces, dashes or special characters
                    phone = re.sub(r'[^0-9+]', '', phone)
                    # Add +91 if not present and it's a 10-digit number
                    if len(phone) == 10 and not phone.startswith('+'):
                        phone = f"+91{phone}"
                
                if phone and name and date and time:
                    # Format the confirmation message
                    message = f"""🎉 *Site Visit Booking Confirmed!*

Dear {name},

Thank you for booking a site visit at Brookstone. Your appointment details:

📅 Date: {date}
⏰ Time: {time}
🏠 Unit Interest: {unit}
💰 Budget Range: {budget}

📍 *Location:*
Brookstone Show Flat
B/S, Vaikunth Bungalows, Next to Oxygen Park
DPS-Bopal Road, Shilaj, Ahmedabad - 380059

Our team will be ready to welcome you! Please carry a valid ID proof.

Need to reschedule? Contact us at: +91 1234567890

Looking forward to showing you your future home! 🌟

_Note: You'll receive a reminder message 1 day before your visit._"""

                    # Send WhatsApp confirmation
                    if send_whatsapp_text(phone, message):
                        # Update status to confirmed
                        row_num = all_records.index(record) + 2  # +2 because sheet is 1-indexed and we have header row
                        sheet.update_cell(row_num, sheet.find('Status').col, 'Confirmed')
                        logging.info(f"✅ Site visit confirmed for {name} on {date} at {time}")
                    else:
                        sheet.update_cell(row_num, sheet.find('Status').col, 'Pending - WhatsApp Failed')
        
        return True
    
    except Exception as e:
        logging.error(f"Error checking new bookings: {e}")
        return False


def extract_budget_from_text(text):
    """Extract budget information from user text"""
    patterns = [
        r'(\d+\.?\d*)\s*(?:cr|crore|crores)',
        r'(\d+\.?\d*)\s*(?:lakh|lakhs)',
        r'₹\s*(\d+\.?\d*)\s*(?:cr|crore|crores)',
        r'₹\s*(\d+\.?\d*)\s*(?:lakh|lakhs)',
    ]
    
    text_lower = text.lower()
    
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            amount = float(match.group(1))
            if 'cr' in pattern or 'crore' in pattern:
                return f"{amount} Cr"
            elif 'lakh' in pattern:
                return f"{amount} Lakh"
    
    return None


# ===== GEMINI AI LOGIC (from appq_gemini.py) =====
def extract_relevant_data(user_question, faq_data, language='english'):
    """Extract only relevant data based on user question to reduce API payload"""
    lang_data = faq_data.get(language, faq_data.get('english', {}))
    relevant_data = {}
    user_question_lower = user_question.lower()
    
    # Always include basic project info
    if 'project_info' in lang_data:
        relevant_data['project_info'] = lang_data['project_info']
        
    # Check for ground floor and general facility queries
    ground_floor_keywords = [
        'ground floor', 'ground level', 'ground', 'facility', 'amenity', 
        'foyer', 'entrance', 'gym', 'library', 'toddler', 'children', 'play',
        'lift', 'elevator', 'stair', 'parking', 'society office', 'reception'
    ]
    
    # Convert to Gujarati keywords for better matching
    gujarati_keywords = [
        'ગ્રાઉન્ડ', 'નીચલો માળ', 'ફોયર', 'પ્રવેશ', 'જીમ', 'લાઇબ્રેરી', 
        'બાળકો', 'રમત', 'લિફ્ટ', 'એલિવેટર', 'સીડી', 'પાર્કિંગ', 
        'સોસાયટી ઓફિસ', 'રિસેપ્શન', 'સુવિધા'
    ]
    
    # Add keywords for flat configurations
    flat_keywords = [
        '3bhk', '3 bhk', '3-bhk', 'three bedroom', 'three bed', 
        '4bhk', '4 bhk', '4-bhk', 'four bedroom', 'four bed'
    ]
    
    # Gujarati flat keywords
    gujarati_flat_keywords = [
        'ત્રણ બેડરૂમ', '૩ બીએચકે', 'ત્રણ બીએચકે',
        'ચાર બેડરૂમ', '૪ બીએચકે', 'ચાર બીએચકે'
    ]
    
    all_keywords = ground_floor_keywords + gujarati_keywords + flat_keywords + gujarati_flat_keywords
    
    if any(word in user_question_lower for word in all_keywords):
        if any(k in user_question_lower for k in ['3bhk', '3 bhk', '3-bhk', 'three bedroom', 'ત્રણ બેડરૂમ', '૩ બીએચકે', 'ત્રણ બીએચકે']):
            if '3bhk_unit_plan' in lang_data:
                relevant_data['unit_plan'] = lang_data['3bhk_unit_plan']
                relevant_data['pricing'] = {'3bhk': lang_data.get('pricing', {}).get('box_price_2650')}
                relevant_data['parking'] = {'3bhk': lang_data.get('parking', {}).get('3bhk_parking')}
        
        elif any(k in user_question_lower for k in ['4bhk', '4 bhk', '4-bhk', 'four bedroom', 'ચાર બેડરૂમ', '૪ બીએચકે', 'ચાર બીએચકે']):
            if '4bhk_unit_plan' in lang_data:
                relevant_data['unit_plan'] = lang_data['4bhk_unit_plan']
                relevant_data['pricing'] = {'4bhk': lang_data.get('pricing', {}).get('box_price_3850')}
                relevant_data['parking'] = {'4bhk': lang_data.get('parking', {}).get('4bhk_parking')}
        
        if 'ground_floor_plan' in lang_data:
            relevant_data['ground_floor_plan'] = lang_data['ground_floor_plan']
        if 'construction_specifications' in lang_data and 'elevator' in lang_data['construction_specifications']:
            relevant_data['elevator'] = lang_data['construction_specifications']['elevator']
    
    # Check for ground floor related queries
    if any(word in user_question_lower for word in ['ground floor', 'ground level', 'ground', 'foyer', 'entrance', 'multipurpose', 'court', 'gym', 'library', 'toddler', 'society', 'seating', 'lift', 'stair', 'amenity', 'facility', 'drop']):
        if 'ground_floor_plan' in lang_data:
            # Add overall ground floor summary
            relevant_data['ground_floor_summary'] = lang_data['ground_floor_plan'].get('summary', '')
            relevant_data['ground_floor_overview'] = lang_data['ground_floor_plan'].get('site_overview', {})
            
            # Always include zone-specific details for comprehensive ground floor queries
            block_a_zone = lang_data['ground_floor_plan'].get('block_a_zone', {})
            block_b_zone = lang_data['ground_floor_plan'].get('block_b_zone', {})
            central_zone = lang_data['ground_floor_plan'].get('central_amenities', {})
            
            # Right Side (Block A Zone) Details
            if 'block a' in user_question_lower or any(word in user_question_lower for word in ['society', 'toddler', 'right side', 'security']):
                relevant_data['block_a_zone'] = {
                    'foyer': {
                        'size': '14\'-9\" × 14\'-6\"',
                        'function': 'Entry point into Block A',
                        'features': 'Staircases (UP & DN) on both sides'
                    },
                    'lift_lobby': {
                        'size': '8\'-5\" × 6\'-8\"',
                        'lifts_count': '2 lifts'
                    },
                    'seating_space': {
                        'size': '44\'-0\" × 21\'-7\"',
                        'function': 'Large sitting lounge outside Block A'
                    },
                    'society_office': {
                        'size': '10\'-3\" × 16\'-3\"',
                        'location': 'Beside the seating lounge'
                    },
                    'store_society': {
                        'size': '10\'-3\" × 5\'-0\"',
                        'location': 'Near the Society Office'
                    },
                    'toddlers_space': {
                        'size': '16\'-4\" × 15\'-9\"',
                        'function': 'Play area for toddlers'
                    },
                    'store_toddlers': {
                        'size': '11\'-0\" × 5\'-7\"',
                        'location': 'Near the Toddler\'s Space'
                    },
                    'toilet_toddlers': {
                        'size': '6\'-0\" × 6\'-7\"',
                        'location': 'Near the Toddler\'s Space'
                    },
                    'other_utilities': {
                        'security': 'Security Cabin with toilet at entry/exit gate',
                        'meter_room': 'Dedicated space for electrical meters',
                        'parking': 'Car parking spaces available',
                        'drop_off': 'Kids drop-off area',
                        'ramp': 'Basement ramp near Block A foyer',
                        'water_feature': 'Decorative water body beside walkway'
                    }
                }
            
            # Left Side (Block B Zone) Details
            if 'block b' in user_question_lower or any(word in user_question_lower for word in ['gym', 'library', 'left side']):
                relevant_data['block_b_zone'] = {
                    'foyer': {
                        'size': '14\'-0\" × 19\'-6\"',
                        'function': 'Entry point into Block B',
                        'features': 'Staircases (UP & DN) on both sides'
                    },
                    'lift_lobby': {
                        'size': '6\'-8\" × 8\'-0\"',
                        'lifts_count': '2 lifts'
                    },
                    'gym': {
                        'size': '17\'-9\" × 19\'-3\"',
                        'function': 'Fitness and exercise area'
                    },
                    'library_lounge': {
                        'size': '18\'-9\" × 26\'-5\"',
                        'function': 'Library, Lounge, and Multi-Purpose Room'
                    },
                    'other_utilities': {
                        'sand_pit': 'Children\'s play area',
                        'postal': 'Dedicated space for postal services'
                    }
                }
            
            # Central Amenities Details
            if any(word in user_question_lower for word in ['central', 'amenity', 'court', 'sand pit', 'lawn', 'facility', 'fountain']):
                relevant_data['central_amenities'] = {
                    'multipurpose_court': {
                        'size': '40\'-8\" × 18\'-11\"',
                        'location': 'Center of complex',
                        'features': 'Surrounded by walkways'
                    },
                    'sand_pit': {
                        'location': 'Adjacent to multipurpose court',
                        'function': 'Children\'s play area'
                    },
                    'other_facilities': [
                        'Internal Roads',
                        'Drop-Off Plaza',
                        'Lawn Area',
                        'DG Set',
                        'Seating Blocks',
                        'Ramp Down to Basement',
                        'Water Fountain with sculpture'
                    ]
                }

    # Check for unit configurations and sizes
    if any(word in user_question_lower for word in ['3bhk', '3 bhk', 'price', 'cost', 'bhk', 'bedroom', 'size', 'sqft', 'configuration', 'apartment', 'flat', 'carpet', 'area', 'dimension']):
        # Always include both configurations
        if 'unit_configurations' in lang_data:
            configs = lang_data['unit_configurations']
            relevant_data['unit_details'] = {
                '3bhk': {
                    'total_size': next((config['size_sqft'] for config in configs if config['type'] == '3BHK'), ''),
                    'carpet_area': next((config['carpet_area'] for config in configs if config['type'] == '3BHK'), ''),
                    'size_yard': next((config['size_sq_yard'] for config in configs if config['type'] == '3BHK'), ''),
                    'price': next((config['price_cr'] for config in configs if config['type'] == '3BHK'), '')
                },
                '4bhk': {
                    'total_size': next((config['size_sqft'] for config in configs if config['type'] == '4BHK'), ''),
                    'carpet_area': next((config['carpet_area'] for config in configs if config['type'] == '4BHK'), ''),
                    'size_yard': next((config['size_sq_yard'] for config in configs if config['type'] == '4BHK'), ''),
                    'price': next((config['price_cr'] for config in configs if config['type'] == '4BHK'), '')
                }
            }
        
        # For detailed floor plans
        if '3bhk_unit_plan' in lang_data and ('3bhk' in user_question_lower or '3 bhk' in user_question_lower or 'carpet' in user_question_lower):
            relevant_data['3bhk_details'] = {
                'overview': lang_data['3bhk_unit_plan']['overview'],
                'special_features': lang_data['3bhk_unit_plan']['special_features'],
                'area_breakdown': lang_data['3bhk_unit_plan']['area_breakdown']
            }
        
        if '4bhk_unit_plan' in lang_data and ('4bhk' in user_question_lower or '4 bhk' in user_question_lower or 'carpet' in user_question_lower):
            relevant_data['4bhk_details'] = {
                'overview': lang_data['4bhk_unit_plan']['overview'],
                'special_features': lang_data['4bhk_unit_plan']['special_features'],
                'area_breakdown': lang_data['4bhk_unit_plan']['area_breakdown']
            }
        
        if 'pricing' in lang_data:
            relevant_data['pricing'] = lang_data['pricing']
    
    if any(word in user_question_lower for word in ['kitchen', 'room', 'bedroom', 'living', 'dining', 'bathroom', 'toilet', 'balcony']):
        if '3bhk_unit_plan' in lang_data:
            relevant_data['3bhk_unit_plan'] = lang_data['3bhk_unit_plan']
        if '4bhk_unit_plan' in lang_data:
            relevant_data['4bhk_unit_plan'] = lang_data['4bhk_unit_plan']
    
    # Check for elevator related queries
    if any(word in user_question_lower for word in ['elevator', 'lift']):
        if 'elevator' in lang_data:
            relevant_data['elevator'] = lang_data['elevator']

    # Check for parking related queries
    if any(word in user_question_lower for word in ['parking', 'car park', 'vehicle']):
        if 'parking' in lang_data:
            relevant_data['parking'] = lang_data['parking']

    # Check for specifications from the image
    if any(word in user_question_lower for word in ['structure', 'flooring', 'bathroom', 'kitchen', 'elevator', 'electrical', 'doors', 'windows', 'security', 'water', 'specifications', 'features']):
        if 'specifications' in lang_data:
            relevant_data['specifications'] = lang_data['specifications']

    # Check for elevator related queries
    if any(word in user_question_lower for word in ['elevator', 'lift', 'lifts']):
        if 'construction_specifications' in lang_data and 'elevator' in lang_data['construction_specifications']:
            relevant_data['elevator'] = lang_data['construction_specifications']['elevator']
        if 'ground_floor_plan' in lang_data:
            block_a_lifts = lang_data['ground_floor_plan']['block_a_zone'].get('lift_lobby', {})
            block_b_lifts = lang_data['ground_floor_plan']['block_b_zone'].get('lift_lobby', {})
            relevant_data['elevators_detail'] = {
                'block_a': block_a_lifts,
                'block_b': block_b_lifts
            }

    # Check for parking related queries
    if any(word in user_question_lower for word in ['parking', 'car park', 'vehicle', 'cars']):
        if 'parking' in lang_data:
            relevant_data['parking'] = lang_data['parking']

    # Check for specifications from the image
    if any(word in user_question_lower for word in ['structure', 'flooring', 'bathroom', 'kitchen', 'electrical', 'doors', 'windows', 'security', 'water', 'specifications', 'features']):
        if 'construction_specifications' in lang_data:
            relevant_data['specifications'] = lang_data['construction_specifications']

    if any(word in user_question_lower for word in ['amenity', 'amenities', 'facility', 'gym', 'pool', 'park', 'club']):
        if 'amenities' in lang_data:
            relevant_data['amenities'] = lang_data['amenities']
    
    if any(word in user_question_lower for word in ['location', 'address', 'connectivity', 'metro', 'nearby', 'landmark']):
        if 'location_details' in lang_data:
            relevant_data['location_details'] = lang_data['location_details']
    
    if any(word in user_question_lower for word in ['possession', 'ready', 'completion', 'timeline', 'delivery']):
        if 'possession_details' in lang_data:
            relevant_data['possession_details'] = lang_data['possession_details']
    
    if any(word in user_question_lower for word in ['developer', 'shatranj', 'aarat', 'group', 'company', 'builder']):
        if 'developer_portfolio' in lang_data:
            relevant_data['developer_portfolio'] = lang_data['developer_portfolio']
    
    # If minimal data, add more sections
    if len(relevant_data) <= 2:
        for section in ['unit_configurations', 'pricing', '3bhk_unit_plan', '4bhk_unit_plan', 'amenities', 'location_details']:
            if section in lang_data:
                relevant_data[section] = lang_data[section]
    
    return relevant_data


def create_gemini_prompt(user_question, faq_data, language='english', chat_history=None):
    """Create an optimized prompt for Gemini with only relevant data and conversation context"""
    relevant_data = extract_relevant_data(user_question, faq_data, language)
    
    # Build conversation context
    conversation_context = ""
    if chat_history and len(chat_history) > 0:
        recent_history = chat_history[-4:] if len(chat_history) > 4 else chat_history
        conversation_context = "\n\nRECENT CONVERSATION:\n"
        for msg, is_user in recent_history:
            role = "User" if is_user else "Bot"
            conversation_context += f"{role}: {msg}\n"
    
    prompt = f"""
You are a helpful real estate chatbot for the Brookstone project. Answer user questions based on the provided project data and conversation context. {"Use Gujarati language for responses." if language == 'gujarati' else "Use English language for responses."}

PROJECT DATA:
{json.dumps(relevant_data, indent=2)}{conversation_context}

USER QUESTION: {user_question}

INSTRUCTIONS:
1. ALWAYS use the PROJECT DATA provided above to answer questions
2. Consider the RECENT CONVERSATION context - if user says "yes", "sure", "please", they are responding to your previous question
3. If any detail shows "TBD", say {"આ વિગત હજી નક્કી કરવાની બાકી છે" if language == 'gujarati' else "This detail is yet to be finalized"}
4. Keep responses concise but comprehensive (max 1000 characters for WhatsApp)
5. For possession date, mention {"મે 2027" if language == 'gujarati' else "May 2027"}
6. After answering, ask 1 natural follow-up question to keep conversation going
7. Be conversational and friendly like a real sales agent
8. NEVER suggest WhatsApp links - only provide phone numbers
9. For agent contact, ONLY provide phone number +91 1234567890
10. Format your response for WhatsApp - use emojis and clear structure

11. For ground floor questions:
    - Always mention specific dimensions when available
    - Describe the layout and connections between spaces
    - Include details about amenities and facilities
    - If size/dimension is asked but not available, acknowledge that and provide other relevant details

12. When mentioning sizes or dimensions:
    - Use the exact measurements as provided in the data
    - Format dimensions clearly with proper units (e.g., "14'-9\" × 14'-6\"")
    - For carpet area, specify it's carpet area (કાર્પેટ એરિયા in Gujarati)
    - For total area, specify it's total built-up area (કુલ બિલ્ટ-અપ એરિયા in Gujarati)

13. For BHK queries:
    - Always mention both carpet area and total area
    - Include price when available
    - Specify number of bathrooms and balconies
    - Mention key features of the layout
    - If asking about 3BHK, provide 3BHK details first, then briefly mention 4BHK is also available
    - If asking about 4BHK, provide 4BHK details first, then briefly mention 3BHK is also available

14. Language-specific formatting:
    - Use native number format for Gujarati (૧,૨,૩,૪,૫,૬,૭,૮,૯,૦)
    - Use appropriate units: {'ચો.ફૂટ for sqft, કરોડ for crore' if language == 'gujarati' else 'sq ft for area, Cr for crore'}
    - Use native terms for amenities and facilities when in Gujarati

ANSWER:"""
    
    return prompt


def call_gemini_api(prompt, language='english'):
    """Call Google Gemini API with retry logic"""
    if not GEMINI_API_KEY:
        return "⚠️ Please configure your Gemini API key"
    
    headers = {'Content-Type': 'application/json'}
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 800
        }
    }
    
    for attempt in range(2):
        try:
            if attempt > 0:
                time.sleep(2)
            
            response = requests.post(
                f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
                headers=headers,
                json=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if 'candidates' in result and len(result['candidates']) > 0:
                    candidate = result['candidates'][0]
                    if 'content' in candidate and 'parts' in candidate['content']:
                        return candidate['content']['parts'][0]['text']
            
            logging.warning(f"Gemini API error: {response.status_code}")
                    
        except Exception as e:
            logging.error(f"Gemini API exception: {e}")
            continue
    
    return "Sorry, I'm having trouble answering right now. Please try again or contact our agent at +91 1234567890."


# ===== MESSAGE PROCESSING LOGIC =====
def process_incoming_message(from_phone, message_text, message_id):
    """Process incoming WhatsApp message and generate response"""
    
    # Get or create user state
    if from_phone not in CONV_STATE:
        CONV_STATE[from_phone] = {
            'chat_history': [],
            'lead_capture_mode': None,
            'user_phone': from_phone,
            'language': 'english',
            'asked_about_brochure': False,
            'booking_info': {}
        }
    
    state = CONV_STATE[from_phone]
    user_lower = message_text.lower().strip()
    
    # Detect language from user's message
    detected_lang = detect_language(message_text)
    state['language'] = detected_lang  # Update user's preferred language
    
    # Add user message to history
    state['chat_history'].append((message_text, True))
    
    # ===== HANDLE PHONE NUMBER FOR BROCHURE =====
    if state.get('lead_capture_mode') == 'phone_for_brochure':
        phone_pattern = r'\b(?:\+91[\s-]?)?[6-9]\d{9}\b'
        phone_match = re.search(phone_pattern, message_text)
        
        if phone_match:
            phone_number = phone_match.group().replace(' ', '').replace('-', '')
            state['user_phone'] = phone_number
            state['lead_capture_mode'] = None
            
            success = send_whatsapp_document(phone_number, BROCHURE_MEDIA_ID)
            
            if not success:
                reply = """I apologize, but there was an issue sending the brochure to your WhatsApp. 

Please try again later or contact our agent directly at +91 1234567890."""
                state['chat_history'].append((reply, False))
                return reply
                
            return None
        else:
            reply = """I didn't find a valid phone number. Please share your *10-digit mobile number* to send the brochure.

For example: 9876543210 or +91 9876543210"""
            state['chat_history'].append((reply, False))
            return reply
    
    # ===== DETECT BROCHURE REQUEST =====
    brochure_keywords = ['brochure', 'pdf', 'download', 'send brochure', 'share brochure', 'floor plan', 'send pdf']
    if any(kw in user_lower for kw in brochure_keywords):
        state['asked_about_brochure'] = True
        
        # Send brochure directly to the phone number that messaged us
        success = send_whatsapp_document(from_phone, BROCHURE_MEDIA_ID)
        
        if not success:
            reply = """I apologize, but there was an issue sending the brochure.

Please contact our agent at +91 1234567890 for assistance."""
            state['chat_history'].append((reply, False))
            return reply
            
        return None
    
    # ===== HANDLE AFFIRMATIVE RESPONSE TO BROCHURE =====
    if state.get('asked_about_brochure', False):
        state['asked_about_brochure'] = False
        
        affirmative_patterns = ['yes', 'yeah', 'yup', 'sure', 'ok', 'okay', 'please', 'send', 'want', 'need']
        
        if any(a in user_lower for a in affirmative_patterns):
            success = send_whatsapp_document(from_phone, BROCHURE_MEDIA_ID)
            
            if not success:
                reply = """❌ There was an issue sending your brochure on WhatsApp.
Please contact our agent at +91 1234567890."""
                state['chat_history'].append((reply, False))
                return reply
                
            return None
    
    # ===== HANDLE WHATSAPP CONTACT REQUEST =====
    contact_patterns = ['whatsapp chat', 'whatsapp number', 'agent whatsapp', 'contact agent', 'agent contact', 'talk to agent']
    
    if any(phrase in user_lower for phrase in contact_patterns):
        reply = f"""Great! You can reach our agent, Shatranj, directly on WhatsApp at:

📱 *WhatsApp Number:* +91 1234567890

Our team will respond within 30 minutes during office hours (10 AM - 7 PM).

You can also call on the same number for a phone conversation.

Is there anything else about Brookstone I can help you with? 🏠"""
        
        state['chat_history'].append((reply, False))
        return reply
    
    # ===== HANDLE SITE VISIT BOOKING =====
    booking_keywords_english = ['book site visit', 'schedule visit', 'site visit', 'book appointment', 'visit booking']
    booking_keywords_gujarati = ['સાઇટ વિઝિટ', 'એપોઇન્ટમેન્ટ', 'વિઝિટ બુક', 'મુલાકાત', 'સાઇટ જોવા']
    
    if any(kw in user_lower for kw in booking_keywords_english + booking_keywords_gujarati):
        # Choose form URL based on detected language
        english_form_url = "https://docs.google.com/forms/d/e/1FAIpQLSceds-nIr9vTLHJ0Jl1TOv0DNYGQhb0CtEa2R3mA9Ae3iP8Lg/viewform"
        gujarati_form_url = "https://docs.google.com/forms/d/e/1FAIpQLSdmWOyIDKZ5KU47LhzKUJXwITN40Fn8tV8swuX7IIWFvB72qQ/viewform"
        
        if state['language'] == 'gujarati':
            reply = f"""🏠 *બ્રૂકસ્ટોન સાઇટ વિઝિટ બુકિંગ*

તમારી સાઇટ વિઝિટ શેડ્યૂલ કરવા માટે, નીચેની લિંક પર ક્લિક કરો અને ફોર્મ ભરો:

📝 {gujarati_form_url}

ફોર્મમાં આ માહિતી પૂછવામાં આવશે:
• તમારું નામ
• કોન્ટેક્ટ નંબર
• પસંદગીની તારીખ અને સમય
• યુનિટ પસંદગી
• બજેટ રેન્જ

ફોર્મ સબમિટ કર્યા પછી, તમને 15 મિનિટની અંદર WhatsApp પર કન્ફર્મેશન મેસેજ મળશે.

ફોર્મ ભરવામાં કોઈ મદદ જોઈએ છે? પૂછવામાં સંકોચ ન કરશો! 😊

_નોંધ: કૃપા કરીને ફોર્મમાં સાચો કોન્ટેક્ટ નંબર આપશો, કારણ કે અમે એ જ WhatsApp નંબર પર કન્ફર્મેશન મોકલીશું._ 📱"""
        else:
            reply = f"""🏠 *Book Your Site Visit to Brookstone*

To schedule your site visit, please click the link below and fill out a quick form:

📝 {english_form_url}

The form will ask for:
• Your Name
• Contact Number
• Preferred Date & Time
• Unit Type Interest
• Budget Range

Once you submit the form, you will receive a confirmation message here on WhatsApp within 15 minutes.

Need help with the form? Feel free to ask! 😊

_Tip: Make sure to provide accurate contact details in the form as we'll send the confirmation on the same WhatsApp number._ 📱"""
        
        state['chat_history'].append((reply, False))
        return reply
        
        state['chat_history'].append((reply, False))
        return reply
    
    # ===== HANDLE BOOKING FORM SUBMISSION =====
    if state.get('lead_capture_mode') == 'booking':
        booking_info = state['booking_info']
        current_step = booking_info.get('current_step')
        
        if current_step == 'name':
            booking_info['name'] = message_text
            booking_info['current_step'] = 'confirm_phone'
            
            reply = f"""Thank you, {message_text}! 📝

I have your phone number as: *{from_phone}*
Is this the correct number for the site visit coordination?

Reply with:
1️⃣ *Yes* to confirm this number
2️⃣ Or type your *alternate number*"""
            
            state['chat_history'].append((reply, False))
            return reply
            
        elif current_step == 'confirm_phone':
            if user_lower == 'yes' or user_lower == '1':
                phone = booking_info['phone']
            else:
                # Check if valid phone number provided
                phone_pattern = r'\b(?:\+91[\s-]?)?[6-9]\d{9}\b'
                phone_match = re.search(phone_pattern, message_text)
                if phone_match:
                    phone = phone_match.group().replace(' ', '').replace('-', '')
                else:
                    reply = """Please provide a valid 10-digit phone number or type *Yes* to confirm the existing number.

Example: 9876543210 or +91 9876543210"""
                    state['chat_history'].append((reply, False))
                    return reply
            
            booking_info['phone'] = phone
            booking_info['current_step'] = 'date'
            
            reply = """Great! Now, please tell me your *preferred date* for the site visit.

Format: DD/MM/YYYY
Example: 05/11/2025"""
            
            state['chat_history'].append((reply, False))
            return reply
            
        elif current_step == 'date':
            # Basic date validation
            date_pattern = r'\d{1,2}[/-]\d{1,2}[/-]\d{4}'
            if not re.match(date_pattern, message_text):
                reply = """Please provide the date in the correct format (DD/MM/YYYY).

Example: 05/11/2025"""
                state['chat_history'].append((reply, False))
                return reply
            
            booking_info['date'] = message_text
            booking_info['current_step'] = 'time'
            
            reply = """Perfect! Now, please select your *preferred time* for the site visit.

Available slots:
1️⃣ 10:00 AM
2️⃣ 11:30 AM
3️⃣ 02:00 PM
4️⃣ 03:30 PM
5️⃣ 05:00 PM

Reply with the slot number (1-5) or type the time."""
            
            state['chat_history'].append((reply, False))
            return reply
            
        elif current_step == 'time':
            time_slots = {
                '1': '10:00 AM',
                '2': '11:30 AM',
                '3': '02:00 PM',
                '4': '03:30 PM',
                '5': '05:00 PM'
            }
            
            if message_text in time_slots:
                time_slot = time_slots[message_text]
            else:
                time_slot = message_text
            
            booking_info['time'] = time_slot
            booking_info['current_step'] = 'unit_type'
            
            reply = """Excellent! Which unit type are you interested in?

1️⃣ *3 BHK* (2650 sq ft)
2️⃣ *4 BHK* (3850 sq ft)
3️⃣ *Both options*

Please reply with 1, 2, or 3."""
            
            state['chat_history'].append((reply, False))
            return reply
            
        elif current_step == 'unit_type':
            unit_types = {
                '1': '3 BHK',
                '2': '4 BHK',
                '3': 'Both 3 & 4 BHK'
            }
            
            if message_text not in ['1', '2', '3']:
                reply = """Please select a valid option:
1️⃣ for 3 BHK
2️⃣ for 4 BHK
3️⃣ for Both options"""
                state['chat_history'].append((reply, False))
                return reply
            
            booking_info['unit_type'] = unit_types[message_text]
            booking_info['current_step'] = 'budget'
            
            reply = """Almost done! 🎯

What is your *approximate budget*?

Example formats:
• 1.5 Cr
• 2 Crore
• 150 Lakhs"""
            
            state['chat_history'].append((reply, False))
            return reply
            
        elif current_step == 'budget':
            budget = extract_budget_from_text(message_text)
            if not budget:
                reply = """Please specify your budget in a clear format:
Example: 1.5 Cr, 2 Crore, or 150 Lakhs"""
                state['chat_history'].append((reply, False))
                return reply
            
            booking_info['budget'] = budget
            
            # Since we're using Google Forms now, redirect to form
            google_form_url = "https://docs.google.com/forms/d/e/1FAIpQLSceds-nIr9vTLHJ0Jl1TOv0DNYGQhb0CtEa2R3mA9Ae3iP8Lg/viewform"
            
            # Clear booking mode
            state['lead_capture_mode'] = None
            
            reply = f"""� To complete your booking, please fill out our site visit form:

📝 {google_form_url}

Once you submit the form, you'll receive a confirmation message with all the details.

Need help with anything else? �"""
            
            state['asked_about_brochure'] = True
            state['chat_history'].append((reply, False))
            return reply
    
    # ===== EXTRACT AND SAVE BUDGET =====
    budget = extract_budget_from_text(message_text)
    if budget and state.get('booking_info'):
        # Since we're now using Google Forms to collect budget
        # Simply log for tracking
        logging.info(f"Budget indicated by {from_phone}: {budget}")
    
    # ===== DEFAULT: USE GEMINI FOR GENERAL QUESTIONS =====
    chat_history = state.get('chat_history', [])
    prompt = create_gemini_prompt(message_text, FAQ_DATA, state['language'], chat_history)
    ai_response = call_gemini_api(prompt, state['language'])
    
    state['chat_history'].append((ai_response, False))
    return ai_response


# ===== WEBHOOK ROUTES =====
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """Webhook verification endpoint for Meta"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    logging.info(f"Webhook verification: mode={mode}, token={token}")
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        logging.info('✅ WEBHOOK VERIFIED')
        return challenge, 200
    else:
        logging.warning('❌ WEBHOOK VERIFICATION FAILED')
        return 'Forbidden', 403


@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint to receive messages from WhatsApp"""
    data = request.get_json()
    
    logging.info(f"Incoming webhook: {json.dumps(data, indent=2)[:500]}...")
    
    try:
        # Parse WhatsApp Cloud API webhook structure
        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                
                # Get messages
                messages = value.get('messages', [])
                for message in messages:
                    from_phone = message.get('from')
                    message_id = message.get('id')
                    msg_type = message.get('type')
                    
                    text = ''
                    
                    if msg_type == 'text':
                        text = message.get('text', {}).get('body', '')
                    elif msg_type == 'button':
                        text = message.get('button', {}).get('text', '')
                    elif msg_type == 'interactive':
                        interactive = message.get('interactive', {})
                        if 'button_reply' in interactive:
                            text = interactive['button_reply'].get('title', '')
                        elif 'list_reply' in interactive:
                            text = interactive['list_reply'].get('title', '')
                    
                    if not text:
                        logging.warning(f"No text found in message type: {msg_type}")
                        continue
                    
                    logging.info(f"📱 Message from {from_phone}: {text}")
                    
                    # Mark message as read
                    mark_message_as_read(message_id)
                    
                    # Process the message and get response
                    response_text = process_incoming_message(from_phone, text, message_id)
                    
                    # Send response back
                    send_whatsapp_text(from_phone, response_text)
    
    except Exception as e:
        logging.exception('❌ Error processing webhook')
    
    return jsonify({'status': 'ok'}), 200


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'whatsapp_configured': bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID),
        'gemini_configured': bool(GEMINI_API_KEY)
    }), 200


@app.route('/', methods=['GET'])
def home():
    """Home endpoint"""
    return jsonify({
        'message': 'Brookstone WhatsApp Bot is running!',
        'endpoints': {
            'webhook': '/webhook',
            'health': '/health'
        }
    }), 200


def check_bookings_periodically():
    """Check for new bookings every 5 minutes"""
    while True:
        try:
            check_new_bookings()
            time.sleep(300)  # Sleep for 5 minutes
        except Exception as e:
            logging.error(f"Error in periodic booking check: {e}")
            time.sleep(60)  # If error occurs, retry after 1 minute

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logging.info(f"🚀 Starting Brookstone WhatsApp Bot on port {port}")
    logging.info(f"WhatsApp configured: {bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)}")
    logging.info(f"Gemini configured: {bool(GEMINI_API_KEY)}")
    
    # Start booking checker in a separate thread
    import threading
    booking_checker = threading.Thread(target=check_bookings_periodically, daemon=True)
    booking_checker.start()
    
    app.run(host='0.0.0.0', port=port, debug=False)
