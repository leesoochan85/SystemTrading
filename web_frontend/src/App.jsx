import { BrowserRouter, Route, Routes } from "react-router-dom";
import DashboardPage from "./pages/DashboardPage";
import StrategyPage from "./pages/StrategyPage";
import "./styles.css";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/strategies/:name" element={<StrategyPage />} />
      </Routes>
    </BrowserRouter>
  );
}
