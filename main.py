from flask import Flask, render_template, request

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def home():
    result = None
    if request.method == "POST":
        cost_price = float(request.form.get("cost_price", 0))
        selling_price = float(request.form.get("selling_price", 0))
        discount = float(request.form.get("discount", 0))

        final_price = selling_price - (selling_price * discount / 100)
        profit = final_price - cost_price
        profit_percent = (profit / cost_price * 100) if cost_price else 0

        result = {
            "final_price": round(final_price, 2),
            "profit": round(profit, 2),
            "profit_percent": round(profit_percent, 2)
        }
    return render_template("index.html", result=result)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
  
