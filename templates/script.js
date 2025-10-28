// Reconnect controls, sharing, and extra polish – ToonEncodes Runner

document.getElementById("btnAbout").addEventListener("click", () => {
  alert("ToonEncodes Runner © 2025\nCreated with ❤️ by Mohanish\nJungle Adventure Theme Edition");
});

document.getElementById("btnHow").addEventListener("click", () => {
  const msg = `
✨ Share Instructions ✨
1️⃣ Click the “Run” button on Replit.
2️⃣ Once the game runs, click the “Share” arrow at top-right.
3️⃣ Choose “Publish” or copy the live link.
4️⃣ Send that link to anyone — they can play instantly!
  
Developed by Mohanish © ToonEncodes
  `;
  document.getElementById("shareBlock").innerText = msg;
});

// Bonus: floating title animation
window.addEventListener("load", () => {
  const h1 = document.querySelector("h1");
  h1.style.transition = "all 1.2s ease";
  h1.style.transform = "translateY(-10px)";
  h1.style.textShadow = "0 0 12px rgba(255,126,179,0.6)";
});

// Auto-start game logic reconnect (from index.html)
document.getElementById("btnStart").addEventListener("click", () => {
  if (typeof startGame === "function") startGame();
});
document.getElementById("btnPause").addEventListener("click", () => {
  if (typeof togglePause === "function") togglePause();
});
