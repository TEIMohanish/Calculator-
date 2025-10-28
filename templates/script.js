const startScreen = document.getElementById('start-screen');
const aboutScreen = document.getElementById('about-screen');
const paymentScreen = document.getElementById('payment-screen');
const gameCanvas = document.getElementById('gameCanvas');
const ctx = gameCanvas.getContext('2d');
const bgMusic = document.getElementById('bgMusic');
const jumpSound = document.getElementById('jumpSound');
const coinSound = document.getElementById('coinSound');

let gameRunning = false;
let timer = null;
let freePlayDuration = 120000; // 2 min
let player = { x: 180, y: 550, size: 50, dy: 0, lane: 1, jump: false };
let obstacles = [];
let coins = [];
let speed = 5;
let score = 0;

document.getElementById('play-btn').onclick = () => startGame();
document.getElementById('about-btn').onclick = () => toggleScreen(aboutScreen);
document.getElementById('back-btn').onclick = () => toggleScreen(startScreen);

function toggleScreen(screen) {
  [startScreen, aboutScreen, paymentScreen].forEach(s => s.classList.add('hidden'));
  screen.classList.remove('hidden');
}

function startGame() {
  [startScreen, aboutScreen, paymentScreen].forEach(s => s.classList.add('hidden'));
  gameCanvas.classList.add('showCanvas');
  gameRunning = true;
  obstacles = [];
  coins = [];
  score = 0;
  player = { x: 180, y: 550, size: 50, dy: 0, lane: 1, jump: false };
  speed = 5;

  // Fix: play bgMusic on user gesture
  bgMusic.volume = 0.3;
  bgMusic.play().catch(() => {
    console.log('Autoplay blocked, will retry after touch.');
    document.body.addEventListener('click', () => bgMusic.play(), { once: true });
  });

  clearTimeout(timer);
  timer = setTimeout(endFreePlay, freePlayDuration);
  requestAnimationFrame(updateGame);
}

function endFreePlay() {
  bgMusic.pause();
  gameRunning = false;
  gameCanvas.classList.remove('showCanvas');
  toggleScreen(paymentScreen);
}

function updateGame() {
  if (!gameRunning) return;
  ctx.clearRect(0, 0, gameCanvas.width, gameCanvas.height);

  drawTracks();
  handleCoins();
  handleObstacles();
  drawPlayer();
  drawScore();

  requestAnimationFrame(updateGame);
}

// ===== Drawing =====
function drawTracks() {
  ctx.fillStyle = '#202f55';
  ctx.fillRect(120, 0, 10, gameCanvas.height);
  ctx.fillRect(260, 0, 10, gameCanvas.height);
}

function drawPlayer() {
  ctx.fillStyle = '#ffcc00';
  ctx.beginPath();
  ctx.arc(player.x + 25, player.y + 25, 25, 0, Math.PI * 2);
  ctx.fill();

  player.y += player.dy;
  if (player.jump) player.dy += 1;
  if (player.y >= 550) {
    player.y = 550;
    player.jump = false;
    player.dy = 0;
  }
}

function drawScore() {
  ctx.fillStyle = '#fff';
  ctx.font = '20px Poppins';
  ctx.fillText(`Score: ${score}`, 10, 30);
}

// ===== Obstacles =====
function handleObstacles() {
  if (Math.random() < 0.02) {
    obstacles.push({
      x: [80, 200, 320][Math.floor(Math.random() * 3)],
      y: -50
    });
  }
  for (let o of obstacles) {
    o.y += speed;
    ctx.fillStyle = '#ff007f';
    ctx.fillRect(o.x, o.y, 40, 40);
    if (Math.abs(o.x - player.x) < 40 && Math.abs(o.y - player.y) < 40) {
      endFreePlay();
      return;
    }
  }
  obstacles = obstacles.filter(o => o.y < gameCanvas.height);
}

// ===== Coins =====
function handleCoins() {
  if (Math.random() < 0.03) {
    coins.push({
      x: [80, 200, 320][Math.floor(Math.random() * 3)],
      y: -40
    });
  }
  for (let c of coins) {
    c.y += speed;
    ctx.fillStyle = '#ffd700';
    ctx.beginPath();
    ctx.arc(c.x + 20, c.y + 20, 10, 0, Math.PI * 2);
    ctx.fill();

    if (Math.abs(c.x - player.x) < 40 && Math.abs(c.y - player.y) < 40) {
      score += 10;
      coinSound.play();
      c.collected = true;
    }
  }
  coins = coins.filter(c => c.y < gameCanvas.height && !c.collected);
}

// ===== Touch Controls =====
let touchStartX = null;
let touchStartY = null;

gameCanvas.addEventListener('touchstart', e => {
  const t = e.touches[0];
  touchStartX = t.clientX;
  touchStartY = t.clientY;
});

gameCanvas.addEventListener('touchend', e => {
  const t = e.changedTouches[0];
  const dx = t.clientX - touchStartX;
  const dy = t.clientY - touchStartY;
  const absX = Math.abs(dx);
  const absY = Math.abs(dy);

  if (absX > absY && absX > 30) {
    if (dx > 0 && player.lane < 2) player.lane++;
    else if (dx < 0 && player.lane > 0) player.lane--;
  } else if (absY > 30 && dy < 0 && !player.jump) {
    jumpSound.play();
    player.dy = -15;
    player.jump = true;
  }
  player.x = 80 + player.lane * 120;
});
