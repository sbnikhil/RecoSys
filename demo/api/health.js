const BACKEND_URL = process.env.BACKEND_URL;

module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');

  if (!BACKEND_URL) {
    return res.status(503).json({ error: 'BACKEND_URL environment variable not set' });
  }

  try {
    const upstream = await fetch(`${BACKEND_URL}/health`);
    const data = await upstream.json();
    res.status(upstream.status).json(data);
  } catch (err) {
    res.status(503).json({ error: err.message });
  }
};
