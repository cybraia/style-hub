import os
import json
from flask import Flask, jsonify, request, render_template
from toolbox_langchain import ToolboxClient
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables (including MONGODB_CONNECTION_STRING and server URL)
load_dotenv()

# --- Setup ---

# Define fallback GCS Base URL (assuming GCS_PRODUCT_BUCKET is in environment)
GCS_BUCKET_NAME = os.getenv('GCS_PRODUCT_BUCKET', 'placeholder-bucket')
GCS_BASE_URL = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}"
FALLBACK_IMAGE_URL = os.getenv('FALLBACK_IMAGE_URL')


# Initialize the MCP Toolbox Client
# This client communicates with the running MCP Toolbox Server (usually on localhost:5000)
TOOLBOX_URL = os.getenv("MCP_TOOLBOX_SERVER_URL")
if not TOOLBOX_URL:
    raise ValueError("MCP_TOOLBOX_SERVER_URL not set in environment.")

try:
    toolbox = ToolboxClient(TOOLBOX_URL)
    print(f"-> MCP Client: Connected to {TOOLBOX_URL}")
    #print(result)
except Exception as e:
    print(f"FATAL ERROR: Could not connect to MCP Toolbox Server. Is the server running? Error: {e}")
    exit()


# Helper function to safely decode data received from the MCP client
def safe_decode_data(data):
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            print(f"Warning: Failed to decode JSON string: {data[:50]}...")
            return None
    return data 

app = Flask(__name__)

# --- New Route to Serve the Frontend ---
@app.route('/')
def index():
    """Renders the main product catalog page."""
    return render_template('index.html')


@app.route('/virtual-tryon')
def virtual_tryon():
    """Renders the virtual try-on page."""
    return render_template('virtual-tryon.html')

# --- Routes ---

@app.route('/products/<product_id>', methods=['GET'])
def get_product(product_id):
    """
    Retrieves a complete product by combining core data (AlloyDB) and details (MongoDB).
    Uses safe decoding to handle string/list/dict variability from MCP tool output.
    """
    
    raw_core = None
    raw_details = None

    # --- 1. FETCH CORE TRANSACTIONAL DATA (AlloyDB) ---
    try:
        core_tool = toolbox.load_tool("get_product_core_data")
        raw_core_response = core_tool.invoke({"product_id": product_id})
        
        # Safely decode the list result
        decoded_core_list = safe_decode_data(raw_core_response)

        # Extract the single dictionary (it's the first element in the list)
        raw_core = decoded_core_list[0] if isinstance(decoded_core_list, list) and decoded_core_list else None
        
    except Exception as e:
        print(f"Warning: AlloyDB core data fetch failed for ID {product_id}. {e}")

    # --- 2. FETCH FLEXIBLE CATALOG DETAILS (MongoDB) ---
    try:
        details_tool = toolbox.load_tool("get_product_details")
        raw_details_response = details_tool.invoke({"product_id": product_id})
        
        # Safely decode the list result
        decoded_details_list = safe_decode_data(raw_details_response)

        # Extract the single dictionary
        raw_details = decoded_details_list[0] if isinstance(decoded_details_list, list) and decoded_details_list else None

    except Exception as e:
        print(f"Warning: MongoDB detail fetch failed for ID {product_id}. {e}")
    
    
    # --- 3. MERGE AND FALLBACK LOGIC ---
    core_data = {} if not raw_core else raw_core
    details_data = {} if not raw_details else raw_details

    if core_data:
        # SCENARIO A/C: AlloyDB Hit. Merge details if found.
        full_product = {**core_data, **details_data} 
        
        if not details_data:
             full_product['source_note'] = 'PARTIAL MODE: MongoDB details missing.'

    elif details_data:
        # SCENARIO B: AlloyDB Miss, MongoDB Hit (Disjoint Fallback)
        
        # Synthesize core fields using MongoDB data
        synth_core = {
            'product_id': details_data.get('product_id'),
            'name': f"MongoDB Product: {details_data.get('category', 'Generic')}", 
            'price': 39.99, 
            'sku': details_data.get('sku', 'SYNTH-001'), 
            'stock': 999,
            'source_note': 'FALLBACK MODE: Core data synthesized from MongoDB details.'
        }
        full_product = {**synth_core, **details_data}

    else:
        # SCENARIO D: Total Miss
        return jsonify({"message": f"Product ID {product_id} not found in any data store."}), 404
        
    
    # --- 4. Final Enrichment (GCS Image URL) ---
    sku = full_product.get('sku', 'N/A')
    
    if sku and sku != 'N/A':
        full_product['image_url'] = f"{GCS_BASE_URL}/{sku}.jpg"
        full_product['fallback_url'] = FALLBACK_IMAGE_URL
    else:
        full_product['image_url'] = FALLBACK_IMAGE_URL
        full_product['fallback_url'] = FALLBACK_IMAGE_URL
        
    
    return jsonify(full_product)



@app.route('/inventory/<category>', methods=['GET'])
def get_category_inventory_stats(category):
    """
    Demonstrates using a single MongoDB Aggregation tool for analytics.
    """
    try:
        stats_tool = toolbox.load_tool("get_product_stats_by_category")
        # The tool requires no parameters, so we invoke with an empty dictionary
        stats_data = stats_tool.invoke({"category": category})
        
        return jsonify({
            "message": "Product statistics successfully aggregated from MongoDB.",
            "statistics": stats_data
        })
        
    except Exception as e:
        return jsonify({"error": "Failed to run category aggregation tool.", "details": str(e)}), 500

# --- Helper to safely load results from MCP tool ---
def safe_load_tool_result(result):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            print(f"Error: Could not decode JSON string from tool result.")
            return []
    return result if isinstance(result, list) else []




@app.route('/products', methods=['GET'])
def list_products():
    """
    Fetches ALL products by concatenating the disjoint AlloyDB and MongoDB catalogs,
    and enriches each item independently.
    """
    
    final_catalog = []
    
    # --- 1. PROCESS ALLOYDB DATA (Core Catalog) ---
    try:
        list_tool = toolbox.load_tool("list_products_core")
        alloydb_products = safe_load_tool_result(list_tool.invoke({}))
        
        for product in alloydb_products:
            sku = product.get('sku')
            
            # Enrich with GCS URL based on AlloyDB SKU
            product['source'] = 'AlloyDB (Core)'
            if sku:
                product['image_url'] = f"{GCS_BASE_URL}/{sku}.jpg"
                # You'd use get_fallback_image(sku) here if the function were defined, 
                # but using a static URL for robustness:
                product['fallback_url'] = FALLBACK_IMAGE_URL 
            else:
                product['image_url'] = FALLBACK_IMAGE_URL
                product['fallback_url'] = FALLBACK_IMAGE_URL
            
            final_catalog.append(product)

    except Exception as e:
        print(f"Error fetching AlloyDB catalog: {e}")
        # We allow the application to proceed even if one source fails

    # --- 2. PROCESS MONGODB DATA (Disjoint Catalog Details) ---
    try:
        details_tool = toolbox.load_tool("list_all_product_details")
        mongodb_products = safe_load_tool_result(details_tool.invoke({}))
        print(mongodb_products)
        for product in mongodb_products:
            # Note: MongoDB documents must have 'product_id' and 'sku' for this to work well
            sku = product.get('sku') 
            product['name'] = product.get('category')
            product['price'] = 39.99
         
            
            # Label the source clearly
            product['source'] = 'MongoDB (Details)'
            
            # Enrich with image data
            if sku:
                product['image_url'] = f"{GCS_BASE_URL}/{sku}.jpg"
                product['fallback_url'] = FALLBACK_IMAGE_URL
            else:
                # If MongoDB data has no SKU, use the placeholder image
                product['image_url'] = FALLBACK_IMAGE_URL
                product['fallback_url'] = FALLBACK_IMAGE_URL
            
            final_catalog.append(product)

    except Exception as e:
        print(f"Error fetching MongoDB catalog: {e}")
        # Allow the application to proceed

    # --- 3. FINAL OUTPUT ---
    if not final_catalog:
         return jsonify({"message": "No products loaded from any source."}), 500

    return jsonify(final_catalog)
        



@app.route('/track/view', methods=['POST'])
def track_user_view():
    """
    Records a user product view event to MongoDB (via MCP Tool).
    This is a high-volume write operation.
    """
    data = request.json
    # Simple validation and default data
    user_id = data.get('user_id', 'User')
    product_id = data.get('product_id')
    event_type = 'product_view'
    
    if not product_id:
        return jsonify({"error": "product_id is required for tracking."}), 400

    try:
        # 1. Load the specific MongoDB insertion tool
        insert_tool = toolbox.load_tool("insert_user_interaction")

        # 2. Prepare the data as a JSON string
        data = {
            "user_id": user_id,
            "product_id": product_id,
            "details": "User viewed this product.",
            "timestamp": datetime.utcnow().isoformat()  # Add timestamp
        }
        data_json = json.dumps(data)

        # 3. Invoke the tool with the data parameter
        response = insert_tool.invoke({"data": data_json})

        # 4. Process the response
        print(response)
        
        return jsonify({
            "message": "Interaction tracked successfully (via MongoDB).",
            "inserted_id": response
        }), 201
        
    except Exception as e:
        print(f"Error while recording user interaction: {e}")
        return jsonify({"error": "Failed to record user interaction.", "details": str(e)}), 500


    
@app.route('/product_by_id', methods=['POST'])
def get_product_by_id():
    """
    Retrieves a single product, prioritizing AlloyDB data and falling back to MongoDB details.
    Includes explicit JSON decoding to prevent the TypeError.
    """
    data = request.json
    user_id = data.get('user_id', 'User')
    product_id = data.get('product_id')
    
    if not product_id:
        return jsonify({"error": "product_id is required."}), 400

    raw_core = None
    raw_details = None

    # --- 1. FETCH CORE TRANSACTIONAL DATA (AlloyDB) ---
    try:
        core_tool = toolbox.load_tool("get_product_core_data")
        raw_core_response = core_tool.invoke({"product_id": product_id})
        
        # Decode the raw response string/list safely
        decoded_core_list = safe_decode_data(raw_core_response)

        # Extract the dictionary (it's the first element in the list)
        raw_core = decoded_core_list[0] if isinstance(decoded_core_list, list) and decoded_core_list else None
        
    except Exception as e:
        print(f"Warning: AlloyDB core data fetch failed for ID {product_id}. {e}")

    # --- 2. FETCH FLEXIBLE CATALOG DETAILS (MongoDB) ---
    try:
        details_tool = toolbox.load_tool("get_product_details")
        raw_details_response = details_tool.invoke({"product_id": product_id})
        
        # Decode the raw response string/list safely
        decoded_details_list = safe_decode_data(raw_details_response)

        # Extract the dictionary (it's the first element in the list)
        raw_details = decoded_details_list[0] if isinstance(decoded_details_list, list) and decoded_details_list else None

    except Exception as e:
        print(f"Warning: MongoDB detail fetch failed for ID {product_id}. {e}")
    
    
    # --- 3. MERGE AND FALLBACK LOGIC ---

    core_data = {} if not raw_core else raw_core
    details_data = {} if not raw_details else raw_details

    # SCENARIO A: Full Merge (The ideal, coherent case) OR SCENARIO C (AlloyDB Hit)
    if core_data:
        # Core data is present. Merge any details found.
        full_product = {**core_data, **details_data} 
        
        # Add source note if details were missing
        if not details_data:
             full_product['source_note'] = 'PARTIAL MODE: MongoDB details missing.'

    elif details_data:
        # SCENARIO B: AlloyDB Miss, MongoDB Hit (The Disjoint Fallback)
        
        # Synthesize required core fields from MongoDB data
        synth_core = {
            'product_id': details_data.get('product_id'),
            'name': f"MongoDB Product: {details_data.get('category', 'Generic')}", 
            'price': 39.99, 
            'sku': details_data.get('sku', 'SYNTH-001'), 
            'stock': 999,
            'source_note': 'FALLBACK MODE: Core data synthesized from MongoDB details.'
        }
        
        # Merge synthesized core with rich MongoDB details
        full_product = {**synth_core, **details_data}

    else:
        # SCENARIO D: Total Miss
        return jsonify({"message": f"Product ID {product_id} not found in any data store."}), 404
        
    
    # --- 4. Final Enrichment (GCS Image URL) ---
    sku = full_product.get('sku', 'N/A')
    
    if sku and sku != 'N/A':
        full_product['image_url'] = f"{GCS_BASE_URL}/{sku}.jpg"
        full_product['fallback_url'] = FALLBACK_IMAGE_URL
    else:
        full_product['image_url'] = FALLBACK_IMAGE_URL
        full_product['fallback_url'] = FALLBACK_IMAGE_URL
        
    
    return jsonify(full_product)


@app.route('/etl/run', methods=['POST'])
def run_etl_to_bigquery():
    """
    Orchestrates the application-driven ETL process:
    1. READ: Aggregates interaction data from MongoDB (using the updated tool).
    2. WRITE: MERGES the resulting summary data into BigQuery (using the new tool).
    """
    try:
        # --- 1. READ/EXTRACT/TRANSFORM (MongoDB via MCP) ---
        mongo_summary_tool = toolbox.load_tool("get_total_interactions_count")
        
        # This returns the aggregated list: [{'product_id': '...', 'interaction_count': N}, ...]
        summary_data = mongo_summary_tool.invoke({"product_id":""})
        
        if not summary_data:
            return jsonify({"message": "No interaction data to transfer."}), 200

        # --- 2. WRITE/LOAD (BigQuery via MCP) ---
        bq_write_tool = toolbox.load_tool("execute_sql_tool")
        
        # Hardcoded JSON string - THIS IS THE KEY STEP
        #hardcoded_json_string = '[{"interaction_count":1,"product_id":"06523234-2a5c-49fb-b801-e18b72ee3578"}]'


        # BigQuery tool execution
        bq_response = bq_write_tool.invoke({"product_summaries": summary_data})
        print(bq_response)
        
        return jsonify({
            "message": "Application-Driven ETL complete. MongoDB summary merged into BigQuery.",
            "products_processed": len(summary_data),
            "bigquery_response": "success" # Contains job/status details
        }), 200

    except Exception as e:
        return jsonify({"error": "ETL orchestration failed.", "details": str(e)}), 500



@app.route('/analytics/top5', methods=['GET'])
def get_top_5_products():
    """
    1. Executes a BigQuery SQL query to get the Top 5 product IDs by total_views.
    2. Fetches core product details (name, price) for those IDs from AlloyDB.
    """
    try:
        # 1. Get Top 5 Product IDs and view counts from BigQuery (via MCP)
        top5_tool = toolbox.load_tool("get_top_5_views")
        top5_response = top5_tool.invoke({})
        
        if not top5_response:
            return jsonify({"message": "No views recorded in BigQuery for ranking."}), 200

        # 2. Extract IDs and orchestrate data lookup (AlloyDB + GCS)
        top_products_list = []
        gcs_base_url = f"https://storage.googleapis.com/{os.getenv('GCS_PRODUCT_BUCKET')}"
     
        # Check if top5_response is a string, and if so, parse it as JSON
        if isinstance(top5_response, str):
            try:
                top5_response = json.loads(top5_response)
            except json.JSONDecodeError as e:
                return jsonify({"error": "Error decoding JSON response from BigQuery tool.", "details": str(e)}), 500
        
        for top_item in top5_response:
            product_id = top_item['product_id']
            print(product_id)
            
            # Fetch combined data (AlloyDB core + MongoDB details) using existing tool
            try:
                core_data_response = toolbox.load_tool("get_product_core_data").invoke({"product_id": product_id})
                
                if isinstance(core_data_response, str):
                    core_data_response = json.loads(core_data_response)
                
                # Access the actual core data
                if core_data_response and isinstance(core_data_response, list) and len(core_data_response) > 0:
                    core_data = core_data_response[0]
                else:
                    core_data = None
                    print(f"Warning: No core data found for product ID: {product_id}")

            except Exception as e:
                print(f"Error fetching core data for product ID {product_id}: {e}")
                core_data = None  # Set to None to prevent further errors

            '''
            try:
                details_data_response = toolbox.load_tool("get_product_details").invoke({"product_id": product_id})
                print(details_data_response)


                #The result of a mongodb-find tool is a list of documents
                 details_data = details_data_response[0] if details_data_response else {}
                
            except Exception as e:
                 print(f"Error fetching details data for product ID {product_id}: {e}")
                 details_data = {}   
            '''
            
            if core_data:
                product = core_data
                
                # Enrich with GCS URL and views
                product['total_views'] = top_item['interaction_score']  # Use 'interaction_score' instead of 'total_views'
                product['image_url'] = f"{gcs_base_url}/thumbnails/{product['sku']}.jpg"
                
                # Add MongoDB details if available
                #if details_data:
                #    product['category'] = details_data.get('category', 'N/A')
                
                top_products_list.append(product)

        return jsonify(top_products_list)
        
    except Exception as e:
        return jsonify({"error": "BigQuery Analytics query failed.", "details": str(e)}), 500




if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False) 
    # NOTE: debug=False is crucial for production environments like Cloud Run
