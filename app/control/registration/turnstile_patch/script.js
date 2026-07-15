(() => {
  const random = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
  const x = random(800, 1200);
  const y = random(400, 600);
  Object.defineProperty(MouseEvent.prototype, "screenX", { value: x });
  Object.defineProperty(MouseEvent.prototype, "screenY", { value: y });
})();
