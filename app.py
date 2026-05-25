from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
from datetime import datetime, timedelta
import json
import base64
import time
import os
import traceback
import certifi
from dotenv import load_dotenv

load_dotenv()

# Use certifi's CA bundle to avoid broken system SSL cert paths
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

app = Flask(__name__)
CORS(app)

# Google Sheets Authentication
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
CREDENTIALS_PATH = os.getenv('GOOGLE_CREDENTIALS_PATH', r'C:\Users\Oduor\Downloads\JSON Files\retention-484110-9e4520124486.json')
SPREADSHEET_ID   = os.getenv('SPREADSHEET_ID', '1zravAS7NoxjnV-2476eBhMitZYQmxWgef3JTbwD-Rag')
CACHE_TTL        = int(os.getenv('CACHE_TIMEOUT', 300))   # seconds; default 5 min

_data_cache = {'data': None, 'ts': 0}

def get_cached_processed_data():
    """Return (merged_df, all_leads, shops_processed), re-fetching only when cache is stale."""
    global _data_cache
    if _data_cache['data'] is not None and (time.time() - _data_cache['ts']) < CACHE_TTL:
        return _data_cache['data']

    shops_df, leads_df, whatsapp_df = get_sheets_data()
    shops_df, leads_df, whatsapp_df = clean_data(shops_df, leads_df, whatsapp_df)
    merged_df, all_leads, shops_processed = merge_data(shops_df, leads_df, whatsapp_df)

    _data_cache['data'] = (merged_df, all_leads, shops_processed)
    _data_cache['ts']   = time.time()
    return _data_cache['data']

def authenticate_sheets():
    """Authenticate with Google Sheets API.

    Credential modes (checked in order):
    1. GOOGLE_CREDENTIALS_B64  - base64-encoded JSON string (safest for cloud env vars)
    2. GOOGLE_CREDENTIALS_JSON - raw JSON string
    3. GOOGLE_CREDENTIALS_PATH - path to a local JSON file (local dev / Render)
    """
    b64_str = os.getenv('GOOGLE_CREDENTIALS_B64')
    if b64_str:
        creds_dict = json.loads(base64.b64decode(b64_str).decode('utf-8'))
        return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPES))

    json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if json_str:
        creds_dict = json.loads(json_str)
        return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPES))

    return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPES))

def worksheet_to_df(worksheet):
    """Convert a worksheet to DataFrame, dropping blank/duplicate header columns."""
    rows = worksheet.get_all_values()
    if not rows:
        return pd.DataFrame()
    headers = rows[0]
    seen = set()
    keep = []
    for i, h in enumerate(headers):
        h = h.strip()
        if h and h not in seen:
            seen.add(h)
            keep.append((i, h))
    if not keep:
        return pd.DataFrame()
    indices, col_names = zip(*keep)
    data = [[row[i] if i < len(row) else '' for i in indices] for row in rows[1:]]
    return pd.DataFrame(data, columns=col_names)

def get_sheets_data():
    """Fetch data from all three sheets"""
    client = authenticate_sheets()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    shops_df = worksheet_to_df(spreadsheet.worksheet('Shops'))
    leads_df = worksheet_to_df(spreadsheet.worksheet('Leads_2025'))
    whatsapp_df = worksheet_to_df(spreadsheet.worksheet('Whatsapp'))

    return shops_df, leads_df, whatsapp_df

def normalize_phone(series):
    """Normalize phone numbers to 254XXXXXXXXX by extracting the last 9 significant digits.
    Handles 0712…, 254712…, +254712…, spaces, dashes — any common Kenyan format."""
    def _norm(p):
        p = str(p).strip()
        if p.lower() in ('nan', 'none', 'null', ''):
            return ''
        digits = ''.join(c for c in p if c.isdigit())
        if len(digits) < 7:
            return digits          # too short; leave as-is
        return '254' + digits[-9:]  # last 9 digits = subscriber number
    return series.apply(_norm)

def clean_data(shops_df, leads_df, whatsapp_df):
    """Clean and prepare data"""
    # Convert date columns
    shops_df['Date'] = pd.to_datetime(shops_df['Date'], errors='coerce')
    leads_df['Date'] = pd.to_datetime(leads_df['Date'], errors='coerce')
    whatsapp_df['DATE'] = pd.to_datetime(whatsapp_df['DATE'], errors='coerce')

    # Normalize contact columns to the same phone format before any merge
    shops_df['Phone']       = normalize_phone(shops_df['Phone'])
    leads_df['CONTACT']     = normalize_phone(leads_df['CONTACT'])
    whatsapp_df['CONTACT']  = normalize_phone(whatsapp_df['CONTACT'])
    
    # Normalize individual ad-spend columns, then combine into MARKETING EXPENSE
    # Strip commas (thousands separators) before numeric conversion
    for col in ['META ADS', 'TIKTOK ADS', 'Price']:
        if col in shops_df.columns:
            cleaned = shops_df[col].astype(str).str.replace(',', '', regex=False).str.strip()
            shops_df[col] = pd.to_numeric(cleaned, errors='coerce')
            if col != 'Price':
                shops_df[col] = shops_df[col].fillna(0)
        elif col != 'Price':
            shops_df[col] = 0.0
    shops_df['MARKETING EXPENSE'] = shops_df['META ADS'] + shops_df['TIKTOK ADS']
    
    return shops_df, leads_df, whatsapp_df

def source_to_channel(source):
    """Group a raw source into a high-level paid/organic channel."""
    s = str(source).strip().lower()
    if 'liz' in s:                                          return 'Meta Ads'
    if any(k in s for k in ['web check', 'checkout']):     return 'Other'
    if any(k in s for k in ['tiktok', 'tik tok', 'tik-tok']): return 'TikTok'
    if any(k in s for k in ['facebook', 'meta', 'instagram', 'ig-', ' ig', 'fb']):
        return 'Meta Ads'
    if any(k in s for k in ['direct', 'organic', 'referral', 'whatsapp', 'google', 'email', 'youtube']):
        return 'Organic'
    return 'Other'

def normalize_source(s):
    """Map raw source strings to canonical group names."""
    if pd.isna(s) or not str(s).strip():
        return 'Unknown'
    sl = str(s).strip().lower()

    if 'direct' in sl:
        return 'Direct'
    if any(k in sl for k in ['facebook', 'meta ad-fb', 'meta ads-fb', 'meta fb', 'meta-fb']):
        return 'Facebook'
    if any(k in sl for k in ['twitter', 'meta ad x', 'meta ad-x', 'meta-x', 'meta x', ' x-', 'twit']):
        return 'Twitter/X'
    if any(k in sl for k in ['tiktok', 'tik tok', 'tik-tok']):
        return 'TikTok'
    if any(k in sl for k in ['instagram', 'meta ad-ig', 'meta ads-ig', 'meta ig', 'meta-ig', 'ig-']):
        return 'Instagram'
    if any(k in sl for k in ['whatsapp', 'watsapp', 'wha', 'w.a']):
        return 'WhatsApp'
    if any(k in sl for k in ['referral', 'refer', 'referal']):
        return 'Referral'
    if any(k in sl for k in ['google', 'goog']):
        return 'Google'
    if any(k in sl for k in ['email', 'e-mail']):
        return 'Email'
    if any(k in sl for k in ['youtube', 'you tube']):
        return 'YouTube'

    return str(s).strip()

def merge_data(shops_df, leads_df, whatsapp_df):
    """Merge all data based on contact as primary key"""
    # Combine lead sources
    leads_df['Lead_Source'] = 'Leads_2025'
    whatsapp_df['Lead_Source'] = 'WhatsApp'
    
    # Merge leads and whatsapp
    all_leads = pd.concat([
        leads_df[['Date', 'CONTACT', 'NAME', 'BRANCH', 'Source', 'Lead_Source']].rename(columns={'Date': 'Date', 'Source': 'Source'}),
        whatsapp_df[['DATE', 'CONTACT', 'NAME', 'SOURCE', 'ACTIVITY', 'BRANCH', 'Lead_Source']].rename(
            columns={'DATE': 'Date', 'SOURCE': 'Source', 'ACTIVITY': 'Activity'}
        )
    ], ignore_index=True)
    
    all_leads['Date']   = pd.to_datetime(all_leads['Date'], errors='coerce')
    all_leads['Source'] = all_leads['Source'].apply(normalize_source)

    # Select only needed purchase columns from shops to avoid column name conflicts
    # (shops_df shares Date, NAME, BRANCH etc. with all_leads)
    shop_cols = ['Phone', 'Price', 'MARKETING EXPENSE', 'META ADS', 'TIKTOK ADS']
    for col in ['Location', 'Date']:
        if col in shops_df.columns:
            shop_cols.append(col)

    shops_purchase = shops_df[shop_cols].rename(columns={'Date': 'Purchase_Date'})

    # Merge with shop data (purchases)
    merged_df = all_leads.merge(
        shops_purchase,
        left_on='CONTACT',
        right_on='Phone',
        how='left'
    )

    return merged_df, all_leads, shops_df

@app.route('/')
def index():
    return render_template('Dashboard.html')

@app.route('/api/dashboard-data', methods=['GET'])
def get_dashboard_data():
    """Main endpoint to get all dashboard data"""
    try:
        # Get filters from request
        time_filter = request.args.get('timeFilter', 'all')
        source_filter = request.args.get('source', 'all')
        location_filter = request.args.get('location', 'all')
        search_query = request.args.get('search', '')
        
        # Fetch and process data (served from cache when fresh)
        merged_df, all_leads, shops_processed = get_cached_processed_data()

        # Apply filters to leads/merged data
        filtered_data = apply_filters(merged_df, time_filter, source_filter, location_filter, search_query)

        # Apply the same time filter to shop purchases so spend, revenue, and
        # conversions all reflect the selected period (not all-time)
        shops_filtered = filter_shops_by_time(shops_processed, time_filter)

        # Calculate all metrics
        dashboard_data = {
            'executiveSummary': calculate_executive_summary(filtered_data, shops_filtered),
            'leadStatusDistribution': calculate_lead_status(filtered_data),
            'marketingMetrics': calculate_marketing_metrics(filtered_data, shops_filtered),
            'activityEffectiveness': calculate_activity_effectiveness(filtered_data, shops_filtered),
            'platformPerformance': calculate_platform_performance(filtered_data),
            'timeToFirstPurchase': calculate_time_to_purchase(merged_df, filtered_data),
            'branchPerformance': calculate_branch_performance(filtered_data),
            'conversionFunnel': calculate_conversion_funnel(all_leads, shops_filtered, filtered_data),
            'unitEconomics': calculate_unit_economics(filtered_data, shops_filtered),
            'adPlatformROI':       calculate_ad_platform_roi(shops_filtered, filtered_data),
            'marketingSourceROI':  calculate_marketing_source_roi(filtered_data, shops_filtered),
            'topCustomersByBranch': calculate_top_customers_by_branch(filtered_data, shops_filtered),
            'branchBreakdown':      calculate_branch_breakdown(filtered_data, shops_filtered),
            'sourceConversionTimes': calculate_source_conversion_times(filtered_data, shops_filtered),
            'leadPurchaseJourney':   calculate_lead_purchase_journey(all_leads, shops_filtered),
            'unconvertedMetrics':    calculate_unconverted_metrics(filtered_data, shops_filtered),
            'filters': {
                'timeOptions': ['weekly', 'monthly', 'quarterly', 'yearly'],
                'sourceOptions': get_unique_sources(all_leads),
                'locationOptions': get_unique_locations(shops_processed),
                'currentFilters': {
                    'timeFilter': time_filter,
                    'source': source_filter,
                    'location': location_filter
                }
            }
        }
        
        return jsonify(dashboard_data)
    
    except Exception as e:
        tb = traceback.format_exc()
        print(tb, flush=True)
        return jsonify({'error': str(e), 'traceback': tb}), 500

def apply_filters(merged_df, time_filter, source_filter, location_filter, search_query):
    """Apply filters to data"""
    df = merged_df.copy()
    
    # Time filter
    if time_filter != 'all':
        today = datetime.now()
        if time_filter == 'weekly':
            start_date = today - timedelta(days=7)
        elif time_filter == 'monthly':
            start_date = today - timedelta(days=30)
        elif time_filter == 'quarterly':
            start_date = today - timedelta(days=90)
        elif time_filter == 'yearly':
            start_date = today - timedelta(days=365)
        
        df = df[df['Date'] >= start_date]
    
    # Source filter
    if source_filter != 'all':
        df = df[df['Source'] == source_filter]
    
    # Location filter
    if location_filter != 'all' and 'Location' in df.columns:
        df = df[df['Location'] == location_filter]
    
    # Search filter
    if search_query:
        df = df[
            (df['NAME'].astype(str).str.contains(search_query, case=False, na=False)) |
            (df['CONTACT'].astype(str).str.contains(search_query, case=False, na=False))
        ]
    
    return df

BRANCH_REGIONS = {
    'Starmall':  'Nairobi CBD',
    'Hazina':    'Nairobi CBD',
    'Hilton':    'Nairobi CBD',
    'Ktda':      'Nairobi CBD',
    'Website':   'Online',
    'Nakuru':    'Rift Valley',
    'Eldoret':   'Rift Valley',
    'Kitengela': 'Rift Valley',
    'Mombasa':   'Coastal Region',
    'Kisumu':    'Western & Nyanza',
    'Kakamega':  'Western & Nyanza',
    'Kisii':     'Western & Nyanza',
    'Thika':     'Central Region',
    'Nanyuki':   'Central Region',
    'Meru':      'Central Region',
    'Sinza':     'Diaspora',
    'Uganda':    'Diaspora',
}

def calculate_branch_breakdown(filtered_data, shops_df):
    """Full branch analytics: leads, WA follow-ups, conversions, activities, revenue."""
    if 'BRANCH' not in filtered_data.columns:
        return []

    shop_phones = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)
    result = []

    for branch in filtered_data['BRANCH'].dropna().unique():
        branch = str(branch).strip()
        if not branch:
            continue

        branch_df = filtered_data[filtered_data['BRANCH'] == branch]
        unique_leads = branch_df['CONTACT'].nunique()

        # WA follow-ups: total WhatsApp rows (one contact can appear many times)
        wa_df = branch_df[branch_df['Lead_Source'] == 'WhatsApp']
        wa_followups     = len(wa_df)
        wa_followups_pct = round(wa_followups / unique_leads * 100) if unique_leads > 0 else 0

        # Conversions via direct intersection
        branch_contacts   = set(p for p in branch_df['CONTACT'].dropna().unique() if len(str(p)) >= 9)
        converted_contacts = branch_contacts.intersection(shop_phones)
        conversions  = len(converted_contacts)
        not_converted = max(0, unique_leads - conversions)
        conv_rate    = round(conversions / unique_leads * 100, 1) if unique_leads > 0 else 0

        # Revenue from time-filtered shops for converted contacts
        revenue = float(shops_df[shops_df['Phone'].isin(converted_contacts)]['Price'].dropna().sum())

        # WA activity breakdown
        activities = []
        if 'Activity' in wa_df.columns:
            for act, cnt in wa_df['Activity'].dropna().value_counts().items():
                if str(act).strip():
                    activities.append({'activity': str(act), 'count': int(cnt)})

        result.append({
            'name':          branch,
            'region':        BRANCH_REGIONS.get(branch, ''),
            'uniqueLeads':   int(unique_leads),
            'waFollowUps':   int(wa_followups),
            'waFollowUpsPct': int(wa_followups_pct),
            'conversions':   int(conversions),
            'notConverted':  int(not_converted),
            'convRate':      conv_rate,
            'revenue':       round(revenue, 2),
            'waActivities':  activities,
        })

    return sorted(result, key=lambda x: x['conversions'], reverse=True)

def get_unique_sources(df):
    """Get unique sources"""
    sources = df['Source'].dropna().unique().tolist()
    return sorted([s for s in sources if s])

def get_unique_locations(df):
    """Get unique locations"""
    if 'Location' not in df.columns:
        return []
    locations = df['Location'].dropna().unique().tolist()
    return sorted([l for l in locations if l])

def calculate_source_conversion_times(filtered_data, shops_df):
    """Per-source and per-channel average / fastest / slowest days from first lead to first purchase."""
    shop_phones = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)
    shop_purchase_dates = shops_df.dropna(subset=['Date']).groupby('Phone')['Date'].min()

    by_source = []
    channel_days = {}

    for source in filtered_data['Source'].dropna().unique():
        src_df   = filtered_data[filtered_data['Source'] == source]
        src_lead = src_df.groupby('CONTACT')['Date'].min()
        converted_idx = src_lead.index[src_lead.index.map(lambda c: c in shop_phones and len(str(c)) >= 9)]
        common = converted_idx.intersection(shop_purchase_dates.index)
        if not len(common):
            continue
        days = (shop_purchase_dates[common] - src_lead[common]).dt.days
        days = days[days >= 0]
        if not len(days):
            continue
        channel = source_to_channel(source)
        by_source.append({
            'source':      str(source),
            'channel':     channel,
            'conversions': int(len(days)),
            'avgDays':     round(float(days.mean()), 1),
            'fastestDays': int(days.min()),
            'slowestDays': int(days.max()),
        })
        channel_days.setdefault(channel, []).extend(days.tolist())

    by_channel = [
        {'channel': ch, 'avgDays': round(sum(dl)/len(dl), 1), 'conversions': len(dl)}
        for ch, dl in channel_days.items() if dl
    ]
    return {
        'bySource':  sorted(by_source,  key=lambda x: x['avgDays']),
        'byChannel': sorted(by_channel, key=lambda x: x['avgDays']),
    }

def calculate_lead_purchase_journey(all_leads, shops_df, limit=500):
    """Individual lead→purchase records: first contact → first sale, sorted longest first."""
    shop_phones = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)

    lead_agg = all_leads.groupby('CONTACT').agg(
        name      =('NAME',   'first'),
        lead_date =('Date',   'min'),
        source    =('Source', 'first'),
    ).reset_index()
    lead_agg = lead_agg[lead_agg['CONTACT'].apply(lambda c: c in shop_phones)]
    if lead_agg.empty:
        return {'records': [], 'total': 0}

    shop_agg = shops_df.dropna(subset=['Date']).groupby('Phone').agg(purchase_date=('Date', 'min')).reset_index()
    if 'Location' in shops_df.columns:
        shop_agg = shop_agg.merge(shops_df.groupby('Phone')['Location'].first().reset_index(), on='Phone', how='left')
    else:
        shop_agg['Location'] = ''

    merged = lead_agg.merge(shop_agg, left_on='CONTACT', right_on='Phone', how='inner')
    merged['days'] = (merged['purchase_date'] - merged['lead_date']).dt.days
    merged = merged[merged['days'] >= 0].sort_values('days', ascending=False)
    total  = len(merged)

    records = []
    for _, row in merged.head(limit).iterrows():
        records.append({
            'name':         str(row['name'])     if pd.notna(row.get('name'))          else '—',
            'phone':        str(row['CONTACT']),
            'leadDate':     row['lead_date'].strftime('%Y-%m-%d')     if pd.notna(row.get('lead_date'))     else None,
            'purchaseDate': row['purchase_date'].strftime('%Y-%m-%d') if pd.notna(row.get('purchase_date')) else None,
            'days':         int(row['days']),
            'shop':         str(row.get('Location','—')) if pd.notna(row.get('Location')) else '—',
            'source':       str(row.get('source','—'))   if pd.notna(row.get('source'))   else '—',
        })
    return {'records': records, 'total': total}

def calculate_unconverted_metrics(filtered_data, shops_df):
    """Full unconverted-lead analysis: counts, buckets, channels, branches, per-lead list."""
    shop_phones      = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)
    all_contacts     = set(p for p in filtered_data['CONTACT'].dropna().unique() if len(str(p)) >= 9)
    converted        = all_contacts.intersection(shop_phones)
    not_conv_set     = all_contacts - converted
    total_leads      = len(all_contacts)
    not_conv_count   = len(not_conv_set)
    drop_off_rate    = round(not_conv_count / total_leads * 100, 1) if total_leads > 0 else 0

    unc_df = filtered_data[filtered_data['CONTACT'].isin(not_conv_set)].copy()
    agg_cols = {'name': ('NAME', 'first'), 'last_date': ('Date', 'max'), 'source': ('Source', 'first')}
    contact_agg = unc_df.groupby('CONTACT').agg(**agg_cols)
    if 'BRANCH' in unc_df.columns:
        contact_agg = contact_agg.join(unc_df.groupby('CONTACT')['BRANCH'].first())

    today = pd.Timestamp(datetime.now())
    contact_agg['days_since'] = (today - contact_agg['last_date']).dt.days.fillna(9999).astype(int)

    ds = contact_agg['days_since']
    buckets = {
        '0-30':    int((ds <= 30).sum()),
        '31-90':   int(((ds > 30)  & (ds <= 90)).sum()),
        '91-180':  int(((ds > 90)  & (ds <= 180)).sum()),
        '181-365': int(((ds > 180) & (ds <= 365)).sum()),
        '365+':    int((ds > 365).sum()),
    }

    contact_agg['channel'] = contact_agg['source'].apply(source_to_channel)
    ch_vc = contact_agg['channel'].value_counts()
    by_channel = [
        {'channel': k, 'count': int(v), 'pct': round(v / not_conv_count * 100, 1) if not_conv_count else 0}
        for k, v in ch_vc.items()
    ]

    by_branch, top_sources = {}, {}
    if 'BRANCH' in contact_agg.columns:
        by_branch = {str(k): int(v) for k, v in
                     contact_agg['BRANCH'].dropna().value_counts().head(10).items() if str(k).strip()}
    top_sources = {str(k): int(v) for k, v in
                   contact_agg['source'].dropna().value_counts().head(10).items() if str(k).strip()}

    leads_list = []
    for contact, row in contact_agg.sort_values('days_since').head(2000).iterrows():
        d = int(row['days_since'])
        leads_list.append({
            'name':            str(row['name'])   if pd.notna(row.get('name'))   else '—',
            'phone':           str(contact),
            'source':          str(row['source']) if pd.notna(row.get('source')) else '—',
            'branch':          str(row.get('BRANCH', '—')) if 'BRANCH' in row and pd.notna(row.get('BRANCH')) else '—',
            'lastContactDate': row['last_date'].strftime('%Y-%m-%d') if pd.notna(row.get('last_date')) else None,
            'daysSinceContact': d,
            'status':          'Hot' if d <= 30 else 'Warm' if d <= 90 else 'Cold',
        })

    return {
        'notConverted':     not_conv_count,
        'totalLeads':       total_leads,
        'dropOffRate':      drop_off_rate,
        'buckets':          buckets,
        'byChannel':        by_channel,
        'byBranch':         by_branch,
        'topSources':       top_sources,
        'leads':            leads_list,
        'totalLeadsInList': len(contact_agg),
    }

def calculate_top_customers_by_branch(filtered_data, shops_df, top_n=10):
    """Return top N customers per branch, ranked by total spend then interactions."""
    if 'BRANCH' not in filtered_data.columns:
        return {}

    result = {}
    for branch in sorted(filtered_data['BRANCH'].dropna().unique()):
        branch = str(branch).strip()
        if not branch:
            continue

        branch_df = filtered_data[filtered_data['BRANCH'] == branch]

        # Aggregate per contact: name, unique-day interaction count, first/last seen.
        # Use nunique on the normalized date (date-only) to avoid inflating counts
        # when a contact has multiple rows on the same day (e.g. bulk lead uploads
        # or a leads row + a WhatsApp row on the same date).
        agg = branch_df.groupby('CONTACT').agg(
            name      =('NAME', 'first'),
            last_seen =('Date', 'max'),
            first_seen=('Date', 'min'),
        ).reset_index()

        agg['interactions'] = (
            branch_df.assign(_day=branch_df['Date'].dt.normalize())
            .groupby('CONTACT')['_day'].nunique()
            .reindex(agg['CONTACT'])
            .values
        )

        # Purchase lookup from shops
        shop_subset       = shops_df[shops_df['Phone'].isin(agg['CONTACT'].values)]
        spend_map         = shop_subset.groupby('Phone')['Price'].sum().to_dict()
        purchase_cnt_map  = shop_subset.groupby('Phone')['Price'].count().to_dict()

        agg['total_spent'] = agg['CONTACT'].map(spend_map).fillna(0.0)
        agg['purchases']   = agg['CONTACT'].map(purchase_cnt_map).fillna(0).astype(int)

        top = agg.sort_values(['total_spent', 'interactions'], ascending=[False, False]).head(top_n)

        result[branch] = [
            {
                'rank':         i + 1,
                'name':         str(row['name']) if pd.notna(row['name']) else 'Unknown',
                'contact':      str(row['CONTACT']),
                'interactions': int(row['interactions']),
                'purchases':    int(row['purchases']),
                'totalSpent':   round(float(row['total_spent']), 2),
                'firstSeen':    row['first_seen'].strftime('%Y-%m-%d') if pd.notna(row['first_seen']) else None,
                'lastSeen':     row['last_seen'].strftime('%Y-%m-%d')  if pd.notna(row['last_seen'])  else None,
            }
            for i, (_, row) in enumerate(top.iterrows())
        ]

    return result

def filter_shops_by_time(shops_df, time_filter):
    """Return a copy of shops_df restricted to the selected time window."""
    if time_filter == 'all':
        return shops_df
    days = {'weekly': 7, 'monthly': 30, 'quarterly': 90, 'yearly': 365}.get(time_filter)
    if days is None:
        return shops_df
    cutoff = datetime.now() - timedelta(days=days)
    return shops_df[shops_df['Date'] >= cutoff]

def calculate_executive_summary(filtered_data, shops_df):
    """Calculate executive summary metrics"""
    unique_leads = filtered_data['CONTACT'].nunique()

    # Per-source unique contacts
    leads_contacts   = set(p for p in filtered_data[filtered_data['Lead_Source'] == 'Leads_2025']['CONTACT'].dropna().unique() if len(str(p)) >= 9)
    wa_contacts      = set(p for p in filtered_data[filtered_data['Lead_Source'] == 'WhatsApp']['CONTACT'].dropna().unique() if len(str(p)) >= 9)
    combined_unique  = len(leads_contacts | wa_contacts)   # union = deduplicated total

    # Converted: direct intersection of filtered lead contacts vs shop phones
    # Exclude phones shorter than 9 chars to prevent empty-string false matches
    lead_contacts = set(p for p in filtered_data['CONTACT'].dropna().unique() if len(str(p)) >= 9)
    shop_phones   = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)
    converted     = len(lead_contacts.intersection(shop_phones))

    engaged_whatsapp = filtered_data[filtered_data['Lead_Source'] == 'WhatsApp']['CONTACT'].nunique()

    # Repeat customers: contacts with 3+ distinct visit dates within the filtered period
    repeat_customers = int(
        (filtered_data.groupby('CONTACT')['Date']
         .apply(lambda x: x.dt.normalize().nunique()) > 2).sum()
    )
    
    # Time to first purchase — vectorized
    purchase_date_col = 'Purchase_Date' if 'Purchase_Date' in filtered_data.columns else 'Date'
    lead_dates_s   = filtered_data.groupby('CONTACT')['Date'].min()
    purchase_df_s  = filtered_data[filtered_data['Price'].notna()]
    avg_time_to_purchase = 0
    if not purchase_df_s.empty and purchase_date_col in purchase_df_s.columns:
        purchase_dates_s = purchase_df_s.groupby('CONTACT')[purchase_date_col].min()
        common_s = lead_dates_s.index.intersection(purchase_dates_s.index)
        if len(common_s):
            days_s = (purchase_dates_s[common_s] - lead_dates_s[common_s]).dt.days
            valid_s = days_s[days_s >= 0]
            avg_time_to_purchase = int(valid_s.mean()) if len(valid_s) else 0
    
    # Ad spend: one value per day (same amount repeats on every transaction row for that day)
    # Use groupby max per day to pick the actual spend value, not the 0-rows
    shops_with_date = shops_df.dropna(subset=['Date'])
    meta_spend   = float(shops_with_date.groupby('Date')['META ADS'].max().sum())   if 'META ADS'    in shops_df.columns else 0.0
    tiktok_spend = float(shops_with_date.groupby('Date')['TIKTOK ADS'].max().sum()) if 'TIKTOK ADS' in shops_df.columns else 0.0
    total_spend   = meta_spend + tiktok_spend
    total_revenue = float(shops_df['Price'].dropna().sum())

    cac = round(total_spend / converted, 2) if converted > 0 else 0
    roi = round(((total_revenue - total_spend) / total_spend * 100), 1) if total_spend > 0 else 0

    conv_rate      = round(converted        / unique_leads * 100, 1) if unique_leads > 0 else 0
    engage_rate    = round(int(engaged_whatsapp) / unique_leads * 100, 1) if unique_leads > 0 else 0
    repeat_rate_pct = round(int(repeat_customers) / unique_leads * 100, 1) if unique_leads > 0 else 0

    return {
        'totalUniqueLead':        unique_leads,
        'uniqueLeads2025':        len(leads_contacts),
        'uniqueLeadsWhatsapp':    len(wa_contacts),
        'uniqueLeadsCombined':    combined_unique,
        'converted':              int(converted),
        'conversionRate':         conv_rate,
        'engagedWhatsapp':        int(engaged_whatsapp),
        'engagementRate':         engage_rate,
        'repeatCustomers':        int(repeat_customers),
        'repeatRate':             repeat_rate_pct,
        'avgTimeToFirstPurchase': avg_time_to_purchase,
        'metaAdsSpend':           round(meta_spend,   2),
        'tiktokAdsSpend':         round(tiktok_spend, 2),
        'totalSpend':             round(total_spend,  2),
        'totalRevenue':           round(total_revenue, 2),
        'cac':                    cac,
        'roi':                    roi,
    }

def calculate_lead_status(filtered_data):
    """Calculate lead status distribution with per-lead details for each category."""
    today       = datetime.now()
    today_ts    = pd.Timestamp(today)
    thirty_ago  = today - timedelta(days=30)
    ninety_ago  = today - timedelta(days=90)

    # Exclude future-dated records (data entry errors) — they have negative days_ago
    valid = filtered_data[filtered_data['Date'] <= today_ts]

    hot_df  = valid[valid['Date'] >= thirty_ago]
    warm_df = valid[(valid['Date'] >= ninety_ago) & (valid['Date'] < thirty_ago)]
    cold_df = valid[valid['Date'] < ninety_ago]

    hot_count  = hot_df['CONTACT'].nunique()
    warm_count = warm_df['CONTACT'].nunique()
    cold_count = cold_df['CONTACT'].nunique()
    total      = hot_count + warm_count + cold_count

    def build_leads(df, limit=150):
        if df.empty:
            return []
        agg_kw = {'name': ('NAME', 'first'), 'last_date': ('Date', 'max'), 'source': ('Source', 'first')}
        agg = df.groupby('CONTACT').agg(**agg_kw)
        if 'BRANCH' in df.columns:
            agg = agg.join(df.groupby('CONTACT')['BRANCH'].first())
        # clip(lower=0) prevents any remaining future-date edge cases showing negative
        agg['days_ago'] = (today_ts - agg['last_date']).dt.days.fillna(9999).astype(int).clip(lower=0)
        agg = agg.sort_values('days_ago').head(limit)
        rows = []
        for contact, row in agg.iterrows():
            rows.append({
                'name':    str(row['name'])   if pd.notna(row.get('name'))   else 'Unknown',
                'phone':   str(contact),
                'source':  str(row['source']) if pd.notna(row.get('source')) else '—',
                'branch':  str(row['BRANCH']) if 'BRANCH' in row.index and pd.notna(row.get('BRANCH')) else '—',
                'daysAgo': int(row['days_ago']),
            })
        return rows

    return {
        'hotLeads':  {'count': int(hot_count),  'percentage': round(hot_count/total*100)  if total else 0, 'leads': build_leads(hot_df)},
        'warmLeads': {'count': int(warm_count), 'percentage': round(warm_count/total*100) if total else 0, 'leads': build_leads(warm_df)},
        'coldLeads': {'count': int(cold_count), 'percentage': round(cold_count/total*100) if total else 0, 'leads': build_leads(cold_df)},
    }

def calculate_marketing_metrics(filtered_data, shops_df):
    """Calculate marketing metrics by source using direct contact intersection for accuracy."""
    shop_phones = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)
    metrics = []

    for source in get_unique_sources(filtered_data):
        source_data = filtered_data[filtered_data['Source'] == source]

        leads = source_data['CONTACT'].nunique()
        source_contacts = set(source_data['CONTACT'].dropna().unique())

        # Converted: direct intersection with shop phones avoids join inflation
        converted = len(source_contacts.intersection(shop_phones))
        conv_rate = round((converted / leads * 100), 2) if leads > 0 else 0

        # Revenue: purchases by converted contacts; deduplicate per contact+date to avoid join inflation
        converted_contacts = source_contacts.intersection(shop_phones)
        revenue_rows = shops_df[shops_df['Phone'].isin(converted_contacts)]
        revenue = float(revenue_rows['Price'].dropna().sum())

        # Spend: not meaningful per-source (platform spend is daily total, not per-lead)
        # Show 0 for organic sources; platform spend shown separately in Ad Platform section
        spend = 0.0
        roi = 0.0

        metrics.append({
            'source': source,
            'leads': int(leads),
            'converted': int(converted),
            'conversionRate': conv_rate,
            'spend': spend,
            'revenue': revenue,
            'roi': roi
        })

    return sorted(metrics, key=lambda x: x['leads'], reverse=True)

def calculate_activity_effectiveness(filtered_data, shops_df):
    """Calculate WhatsApp activity effectiveness using direct intersection for accuracy."""
    whatsapp_data = filtered_data[filtered_data['Lead_Source'] == 'WhatsApp'].copy()

    if 'Activity' not in whatsapp_data.columns:
        return []

    shop_phones = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)
    activities = []

    for activity in whatsapp_data['Activity'].dropna().unique():
        if not str(activity).strip():
            continue
        activity_contacts = set(
            whatsapp_data[whatsapp_data['Activity'] == activity]['CONTACT'].dropna().unique()
        )
        engaged   = len(activity_contacts)
        converted = len(activity_contacts.intersection(shop_phones))
        rate      = round((converted / engaged * 100), 2) if engaged > 0 else 0

        activities.append({
            'activity':  activity,
            'engaged':   engaged,
            'converted': converted,
            'rate':      rate
        })

    return sorted(activities, key=lambda x: x['engaged'], reverse=True)

def calculate_platform_performance(filtered_data):
    """Calculate platform performance"""
    platforms = {}
    
    for source in get_unique_sources(filtered_data):
        source_data = filtered_data[filtered_data['Source'] == source]
        
        leads = source_data['CONTACT'].nunique()
        conversions = source_data[source_data['Price'].notna()]['CONTACT'].nunique()
        
        platforms[source] = {
            'leads': int(leads),
            'conversions': int(conversions),
            'conversionRate': round(conversions / leads * 100, 2) if leads > 0 else 0
        }
    
    return platforms

def calculate_time_to_purchase(merged_df, filtered_data):
    """Calculate time to first purchase distribution — vectorized."""
    purchase_date_col = 'Purchase_Date' if 'Purchase_Date' in filtered_data.columns else 'Date'

    lead_dates    = filtered_data.groupby('CONTACT')['Date'].min()
    purchase_df   = filtered_data[filtered_data['Price'].notna()]

    if purchase_df.empty or purchase_date_col not in purchase_df.columns:
        return {'days': [], 'count': []}

    purchase_dates = purchase_df.groupby('CONTACT')[purchase_date_col].min()
    common         = lead_dates.index.intersection(purchase_dates.index)
    if not len(common):
        return {'days': [], 'count': []}

    days_series = (purchase_dates[common] - lead_dates[common]).dt.days
    times       = days_series[days_series >= 0].tolist()

    if not times:
        return {'days': [], 'count': []}
    
    # Create bins for distribution; -1 as left edge captures same-day (0 days) in first bucket
    bins = [-1, 0, 7, 14, 30, 60, 90, 365, float('inf')]
    bin_labels = ['Same Day', '1-7 days', '8-14 days', '15-30 days', '31-60 days', '61-90 days', '91-365 days', '365+ days']
    
    distribution = pd.cut(times, bins=bins, labels=bin_labels, include_lowest=True).value_counts().sort_index()
    
    return {
        'days': distribution.index.tolist(),
        'count': distribution.values.tolist()
    }

def calculate_branch_performance(filtered_data):
    """Calculate branch performance metrics"""
    branches = []
    
    branch_data = filtered_data.dropna(subset=['BRANCH'])
    
    for idx, branch in enumerate(branch_data['BRANCH'].unique(), 1):
        branch_df = filtered_data[filtered_data['BRANCH'] == branch]
        
        leads = branch_df['CONTACT'].nunique()
        engaged = branch_df[branch_df['Lead_Source'] == 'WhatsApp']['CONTACT'].nunique()
        converted = branch_df[branch_df['Price'].notna()]['CONTACT'].nunique()
        conv_rate = (converted / leads * 100) if leads > 0 else 0
        revenue = branch_df['Price'].sum()
        avg_value = (revenue / converted) if converted > 0 else 0
        
        branches.append({
            'number': idx,
            'branch': branch,
            'leads': int(leads),
            'engaged': int(engaged),
            'converted': int(converted),
            'conversionRate': round(conv_rate, 2),
            'revenue': float(revenue),
            'avgValue': round(avg_value, 2)
        })
    
    return branches

def calculate_conversion_funnel(all_leads, shops_df, filtered_data):
    """Calculate conversion funnel"""
    total_leads = filtered_data['CONTACT'].nunique()
    engaged = filtered_data[filtered_data['Lead_Source'] == 'WhatsApp']['CONTACT'].nunique()
    converted = filtered_data[filtered_data['Price'].notna()]['CONTACT'].nunique()
    repeat_purchase = filtered_data.groupby('CONTACT').size()
    repeat_purchase = (repeat_purchase > 1).sum()
    
    stages = [
        {
            'stage': 'Total Leads',
            'count': int(total_leads),
            'percentOfTotal': 100.0,
            'stageConversionRate': 100.0
        },
        {
            'stage': 'Engaged (WhatsApp)',
            'count': int(engaged),
            'percentOfTotal': round(engaged / total_leads * 100, 2) if total_leads > 0 else 0,
            'stageConversionRate': round(engaged / total_leads * 100, 2) if total_leads > 0 else 0
        },
        {
            'stage': 'Converted',
            'count': int(converted),
            'percentOfTotal': round(converted / total_leads * 100, 2) if total_leads > 0 else 0,
            'stageConversionRate': round(converted / engaged * 100, 2) if engaged > 0 else 0
        },
        {
            'stage': 'Repeat Purchase',
            'count': int(repeat_purchase),
            'percentOfTotal': round(repeat_purchase / total_leads * 100, 2) if total_leads > 0 else 0,
            'stageConversionRate': round(repeat_purchase / converted * 100, 2) if converted > 0 else 0
        }
    ]
    
    return stages

def calculate_unit_economics(filtered_data, shops_df):
    """Calculate unit economics"""
    # Spend: pick max value per day (daily budget recorded once, repeated on every transaction row)
    shops_with_date = shops_df.dropna(subset=['Date'])
    meta_s   = float(shops_with_date.groupby('Date')['META ADS'].max().sum())    if 'META ADS'    in shops_df.columns else 0.0
    tiktok_s = float(shops_with_date.groupby('Date')['TIKTOK ADS'].max().sum()) if 'TIKTOK ADS' in shops_df.columns else 0.0
    total_spend = meta_s + tiktok_s
    total_revenue = shops_df['Price'].dropna().sum()
    unique_customers = filtered_data['CONTACT'].nunique()
    total_leads = filtered_data['CONTACT'].nunique()  # unique leads, not raw rows
    
    overall_roi = ((total_revenue - total_spend) / total_spend * 100) if total_spend > 0 else 0
    cac = (total_spend / unique_customers) if unique_customers > 0 else 0
    cpl = (total_spend / total_leads) if total_leads > 0 else 0
    revenue_per_customer = (total_revenue / unique_customers) if unique_customers > 0 else 0
    
    repeat_purchases = (filtered_data.groupby('CONTACT').size() > 1).sum()
    repeat_rate = (repeat_purchases / unique_customers * 100) if unique_customers > 0 else 0
    
    # Days to conversion — vectorized
    purchase_date_col = 'Purchase_Date' if 'Purchase_Date' in filtered_data.columns else 'Date'
    lead_dates_u   = filtered_data.groupby('CONTACT')['Date'].min()
    purchase_df_u  = filtered_data[filtered_data['Price'].notna()]
    avg_days_to_conversion = 0
    if not purchase_df_u.empty and purchase_date_col in purchase_df_u.columns:
        purchase_dates_u = purchase_df_u.groupby('CONTACT')[purchase_date_col].min()
        common_u = lead_dates_u.index.intersection(purchase_dates_u.index)
        if len(common_u):
            days_u = (purchase_dates_u[common_u] - lead_dates_u[common_u]).dt.days
            valid_u = days_u[days_u >= 0]
            avg_days_to_conversion = int(valid_u.mean()) if len(valid_u) else 0
    
    return {
        'totalMarketingSpend': round(total_spend, 2),
        'overallMarketingROI': round(overall_roi, 2),
        'customerAcquisitionCost': round(cac, 2),
        'costPerLead': round(cpl, 2),
        'revenuePerCustomer': round(revenue_per_customer, 2),
        'repeatPurchaseRate': round(repeat_rate, 2),
        'avgDaysToConversion': avg_days_to_conversion
    }

def calculate_ad_platform_roi(shops_df, filtered_data):
    """Calculate spend and ROI for Meta Ads and TikTok Ads separately."""
    total_revenue = shops_df['Price'].dropna().sum()

    # Pick max per day: spend is daily total repeated on every transaction row
    shops_with_date = shops_df.dropna(subset=['Date'])
    meta_spend   = float(shops_with_date.groupby('Date')['META ADS'].max().sum())    if 'META ADS'    in shops_df.columns else 0.0
    tiktok_spend = float(shops_with_date.groupby('Date')['TIKTOK ADS'].max().sum()) if 'TIKTOK ADS' in shops_df.columns else 0.0

    meta_roi   = round(((total_revenue - meta_spend)   / meta_spend   * 100), 2) if meta_spend   > 0 else 0
    tiktok_roi = round(((total_revenue - tiktok_spend) / tiktok_spend * 100), 2) if tiktok_spend > 0 else 0

    # Leads whose Source matches each platform
    meta_leads   = filtered_data[
        filtered_data['Source'].str.contains(r'Meta|Facebook|Instagram|IG\b|FB\b', case=False, na=False, regex=True)
    ]['CONTACT'].nunique()
    tiktok_leads = filtered_data[
        filtered_data['Source'].str.contains(r'Tik\s*Tok|TikTok', case=False, na=False, regex=True)
    ]['CONTACT'].nunique()

    # Converted leads per platform
    meta_converted   = filtered_data[
        filtered_data['Source'].str.contains(r'Meta|Facebook|Instagram|IG\b|FB\b', case=False, na=False, regex=True) &
        filtered_data['Price'].notna()
    ]['CONTACT'].nunique()
    tiktok_converted = filtered_data[
        filtered_data['Source'].str.contains(r'Tik\s*Tok|TikTok', case=False, na=False, regex=True) &
        filtered_data['Price'].notna()
    ]['CONTACT'].nunique()

    return {
        'meta': {
            'name': 'Meta Ads',
            'platforms': 'Instagram & Facebook',
            'spend': round(float(meta_spend), 2),
            'revenue': round(float(total_revenue), 2),
            'roi': meta_roi,
            'leads': int(meta_leads),
            'converted': int(meta_converted),
        },
        'tiktok': {
            'name': 'TikTok Ads',
            'platforms': 'TikTok',
            'spend': round(float(tiktok_spend), 2),
            'revenue': round(float(total_revenue), 2),
            'roi': tiktok_roi,
            'leads': int(tiktok_leads),
            'converted': int(tiktok_converted),
        },
    }


def calculate_marketing_source_roi(filtered_data, shops_df):
    """Per-channel ROI breakdown: Meta Ads, Organic, TikTok, Other."""
    shop_phones = set(p for p in shops_df['Phone'].dropna().unique() if len(str(p)) >= 9)

    shops_with_date = shops_df.dropna(subset=['Date'])
    meta_spend   = float(shops_with_date.groupby('Date')['META ADS'].max().sum())   if 'META ADS'   in shops_df.columns else 0.0
    tiktok_spend = float(shops_with_date.groupby('Date')['TIKTOK ADS'].max().sum()) if 'TIKTOK ADS' in shops_df.columns else 0.0

    copy = filtered_data.copy()
    copy['channel'] = copy['Source'].apply(source_to_channel)

    result = []
    for ch, grp in copy.groupby('channel'):
        leads     = grp['CONTACT'].nunique()
        contacts  = set(grp['CONTACT'].dropna().unique())
        converted = len(contacts.intersection(shop_phones))
        conv_rate = round(converted / leads * 100, 1) if leads > 0 else 0.0

        rev_rows = shops_df[shops_df['Phone'].isin(contacts.intersection(shop_phones))]
        revenue  = float(rev_rows['Price'].dropna().sum())

        spend = meta_spend if ch == 'Meta Ads' else (tiktok_spend if ch == 'TikTok' else 0.0)
        roi   = round((revenue - spend) / spend * 100, 1) if spend > 0 else None
        cpl   = round(spend / leads, 2) if spend > 0 and leads > 0 else None

        result.append({
            'channel':    ch,
            'leads':      int(leads),
            'converted':  int(converted),
            'convRate':   conv_rate,
            'spend':      round(spend, 2),
            'revenue':    round(revenue, 2),
            'roi':        roi,
            'costPerLead': cpl,
        })

    result.sort(key=lambda x: x['leads'], reverse=True)
    return result


@app.route('/api/customer-lookup', methods=['GET'])
def customer_lookup():
    """Return full history, spend stats and lifetime value for a single customer."""
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({'found': False, 'message': 'No query provided'}), 400

        _, all_leads, shops_df = get_cached_processed_data()

        # Decide if input looks like a phone number
        digits = ''.join(c for c in query if c.isdigit())
        is_phone = len(digits) >= 7

        if is_phone:
            norm = '254' + digits[-9:] if len(digits) >= 9 else digits
            lead_mask = all_leads['CONTACT'] == norm
            shop_mask = shops_df['Phone'] == norm
        else:
            lead_mask = all_leads['NAME'].astype(str).str.contains(query, case=False, na=False)
            if 'NAME' in shops_df.columns:
                shop_mask = shops_df['NAME'].astype(str).str.contains(query, case=False, na=False)
            else:
                shop_mask = pd.Series(False, index=shops_df.index)

        matched_leads = all_leads[lead_mask]
        matched_shops = shops_df[shop_mask]

        if matched_leads.empty and matched_shops.empty:
            return jsonify({'found': False, 'message': 'No customer found matching that name or phone number'}), 200

        # Collect all contact numbers found, then expand to full history
        contacts = set()
        if not matched_leads.empty:
            contacts.update(matched_leads['CONTACT'].dropna().unique())
        if not matched_shops.empty:
            contacts.update(matched_shops['Phone'].dropna().unique())
        contacts = {c for c in contacts if len(str(c)) >= 9}

        all_lead_rows  = all_leads[all_leads['CONTACT'].isin(contacts)].copy()
        all_shop_rows  = shops_df[shops_df['Phone'].isin(contacts)].copy()

        # Basic identity
        name    = all_lead_rows['NAME'].dropna().iloc[0] if not all_lead_rows.empty and 'NAME' in all_lead_rows.columns else 'Unknown'
        contact = next(iter(contacts), query)

        # Interaction history (all lead/WhatsApp rows, sorted by date)
        interactions = []
        for _, row in all_lead_rows.sort_values('Date').iterrows():
            interactions.append({
                'date':        row['Date'].strftime('%Y-%m-%d') if pd.notna(row.get('Date')) else None,
                'source':      str(row.get('Source', '')),
                'activity':    str(row['Activity']) if 'Activity' in row and pd.notna(row.get('Activity')) else '',
                'lead_source': str(row.get('Lead_Source', '')),
                'branch':      str(row['BRANCH']) if 'BRANCH' in row and pd.notna(row.get('BRANCH')) else '',
            })

        # Purchase history (all shop rows, sorted by date)
        purchases = []
        total_spent = 0.0
        for _, row in all_shop_rows.sort_values('Date').iterrows():
            price = float(row['Price']) if pd.notna(row.get('Price')) else 0.0
            total_spent += price
            purchases.append({
                'date':     row['Date'].strftime('%Y-%m-%d') if pd.notna(row.get('Date')) else None,
                'amount':   round(price, 2),
                'location': str(row['Location']) if 'Location' in row and pd.notna(row.get('Location')) else '',
            })

        n_purchases   = len(purchases)
        avg_spend     = round(total_spent / n_purchases, 2) if n_purchases else 0.0
        first_seen    = all_lead_rows['Date'].min() if not all_lead_rows.empty else None
        last_seen     = all_lead_rows['Date'].max() if not all_lead_rows.empty else None
        first_purchase_dt = all_shop_rows['Date'].min() if not all_shop_rows.empty else None

        days_as_customer = int((last_seen - first_seen).days) if (
            first_seen is not None and last_seen is not None
            and pd.notna(first_seen) and pd.notna(last_seen)
        ) else 0

        days_to_first_purchase = int((first_purchase_dt - first_seen).days) if (
            first_purchase_dt is not None and first_seen is not None
            and pd.notna(first_purchase_dt) and pd.notna(first_seen)
        ) else None

        return jsonify({
            'found': True,
            'customer': {'name': name, 'contact': contact},
            'stats': {
                'totalInteractions':     len(interactions),
                'totalPurchases':        n_purchases,
                'totalSpent':            round(total_spent, 2),
                'avgSpend':              avg_spend,
                'lifetimeValue':         round(total_spent, 2),
                'firstSeen':             first_seen.strftime('%Y-%m-%d') if first_seen is not None and pd.notna(first_seen) else None,
                'lastSeen':              last_seen.strftime('%Y-%m-%d') if last_seen is not None and pd.notna(last_seen) else None,
                'firstPurchase':         first_purchase_dt.strftime('%Y-%m-%d') if first_purchase_dt is not None and pd.notna(first_purchase_dt) else None,
                'daysAsCustomer':        days_as_customer,
                'daysToFirstPurchase':   days_to_first_purchase,
            },
            'interactions': interactions,
            'purchases':    purchases,
        })
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/debug', methods=['GET'])
def debug_spend():
    """Return raw spend column info to diagnose 0-spend issues."""
    try:
        _, _, shops_df = get_cached_processed_data()
        result = {'all_columns': shops_df.columns.tolist()}
        for col in ['META ADS', 'TIKTOK ADS', 'MARKETING EXPENSE']:
            if col in shops_df.columns:
                non_zero = shops_df[col][shops_df[col] > 0]
                result[col] = {
                    'dtype':        str(shops_df[col].dtype),
                    'non_zero_rows': int(len(non_zero)),
                    'total_sum':    float(shops_df[col].sum()),
                    'max':          float(shops_df[col].max()),
                    'sample_values': non_zero.head(5).tolist()
                }
            else:
                result[col] = 'COLUMN MISSING'
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/refresh', methods=['POST'])
def refresh_cache():
    """Force-clear the data cache so the next request re-fetches from Google Sheets."""
    global _data_cache
    _data_cache = {'data': None, 'ts': 0}
    return jsonify({'status': 'cache cleared'}), 200

@app.route('/api/health', methods=['GET'])
def health_check():
    age = int(time.time() - _data_cache['ts']) if _data_cache['data'] is not None else None
    return jsonify({'status': 'healthy', 'cache_age_seconds': age}), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5005))
    app.run(debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true', port=port, host='0.0.0.0')