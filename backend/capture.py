import time
import httpx
import json
from playwright.sync_api import sync_playwright

def main():
    print("Capturing Swagger UI...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # 1. Swagger UI
        page.goto("http://localhost:8000/docs")
        page.wait_for_selector(".swagger-ui", timeout=30000)
        time.sleep(2) # ensure fully rendered
        page.screenshot(path="../docs/assets/swagger-ui.png")
        print("Saved docs/assets/swagger-ui.png")
        
        # 2. React Dashboard
        print("Navigating to React dashboard...")
        page.goto("http://localhost:3000")
        page.wait_for_selector("input", timeout=30000)
        page.screenshot(path="../docs/assets/dashboard-empty.png")
        
        page.fill("input", "encode/starlette")
        page.click("button[type='submit']")
        
        print("Waiting for analysis results (may take up to 30s)...")
        page.wait_for_selector(".results-card", timeout=60000)
        time.sleep(1) # wait for animations
        page.screenshot(path="../docs/assets/dashboard-results.png")
        print("Saved docs/assets/dashboard-results.png")
        
        browser.close()

    # 3. Get JSON response manually to save
    print("Fetching real JSON response...")
    response = httpx.post("http://localhost:8000/api/analyze", json={"repo": "encode/starlette"})
    task_id = response.json()["task_id"]
    
    while True:
        res = httpx.get(f"http://localhost:8000/api/results/{task_id}")
        data = res.json()
        if data["status"] in ["completed", "failed"]:
            # Clean up dates and task_id to make it deterministic if needed, or just save as is
            with open("../docs/assets/example_response.json", "w") as f:
                json.dump(data, f, indent=2)
            print("Saved docs/assets/example_response.json")
            break
        time.sleep(2)

if __name__ == "__main__":
    main()
