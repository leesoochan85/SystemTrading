const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

async function request(path) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`API ${response.status}: ${body}`);
  }
  return response.json();
}

export const getDashboard = () => request("/api/dashboard");
export const getStrategy = (name) => request(`/api/strategies/${name}`);
export const getVirtualPerformance = (name) =>
  request(`/api/strategies/${name}/virtual-performance`);
export const getVirtualTrades = (name, limit = 100) =>
  request(`/api/virtual/trades?strategy_name=${encodeURIComponent(name)}&limit=${limit}`);
export const getVirtualEvents = (name, limit = 100) =>
  request(`/api/virtual/events?strategy_name=${encodeURIComponent(name)}&limit=${limit}`);
