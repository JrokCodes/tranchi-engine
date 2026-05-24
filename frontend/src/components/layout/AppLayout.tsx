import { Outlet } from 'react-router-dom';
import Navbar from './Navbar';

export default function AppLayout() {
  return (
    <div className="min-h-screen bg-(--color-bg-base)">
      <Navbar />
      <main className="max-w-[1400px] mx-auto px-6 lg:px-10 pt-20 pb-12">
        <Outlet />
      </main>
    </div>
  );
}
