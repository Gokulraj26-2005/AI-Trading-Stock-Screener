import React, { useState, useEffect } from 'react';
import { ChevronLeft, ChevronRight, CandlestickChart, LineChart, LogIn, LogOut, TrendingUp, TrendingDown, ScanLine } from 'lucide-react';

interface SidebarProps {
    activeTab: string;
    onTabChange: (tab: string) => void;
    isCollapsed: boolean;
    toggleCollapse: () => void;
    closeSidebar: () => void;
    onLoginClick: () => void;
    onLogoutClick: () => void;
}

export function Sidebar({ activeTab, onTabChange, isCollapsed, toggleCollapse, closeSidebar, onLoginClick, onLogoutClick }: SidebarProps) {
    const [logoText, setLogoText] = useState('V');

    useEffect(() => {
        if (!isCollapsed) {
            setLogoText('V');
            return;
        }

        const sequence = ['V', 'I', 'A'];
        let index = 0;

        const interval = setInterval(() => {
            index = (index + 1) % sequence.length;
            setLogoText(sequence[index]);
        }, 1000);

        return () => clearInterval(interval);
    }, [isCollapsed]);

const menuItems = [
  { id: 'short-screener', label: 'Short Screener Live', icon: ScanLine, path: '/short-screener' },
  { id: 'chart-analysis', label: 'Chart Analysis', icon: LineChart, path: '/analysis' },
  { id: 'shortlisted', label: 'Live Desk — LONGS', icon: TrendingUp, path: '/shortlisted' },
  { id: 'shortlisted-shorts', label: 'Live Desk — SHORTS', icon: TrendingDown, path: '/shortlisted-shorts' },
  { id: 'charts', label: 'Charts & Order Flow', icon: CandlestickChart, path: '/charts' },
];

    return (
        <div className={`fixed left-4 top-4 bottom-4 glass-panel flex flex-col transition-all duration-300 z-50 rounded-3xl overflow-hidden ${isCollapsed ? 'w-20' : 'w-64'}`}>
            {/* Logo */}
            <div className="h-20 flex items-center px-6 justify-between relative overflow-hidden">
                <div className="absolute inset-0 bg-gradient-to-b from-white/10 to-transparent pointer-events-none" />
                {!isCollapsed && (
                    <span className="text-2xl font-bold bg-gradient-to-r from-primary-400 to-primary-neon bg-clip-text text-transparent drop-shadow-lg">
                        VIA Agent
                    </span>
                )}
                {isCollapsed && (
                    <span className="text-2xl font-bold text-primary-neon mx-auto transition-all duration-500 animate-pulse drop-shadow-neon">
                        {logoText}
                    </span>
                )}
            </div>

            {/* Navigation */}
            <nav className="flex-1 py-6 px-3 space-y-2 overflow-y-auto custom-scrollbar">
                {menuItems.map((item) => {
                    const Icon = item.icon;
                    const isActive = activeTab === item.id;
                    return (
                        <button
                            key={item.id}
                            onClick={() => {
                                onTabChange(item.id);
                                closeSidebar();
                            }}
                            className={`w-full flex items-center px-3 py-3 rounded-xl transition-all duration-300 group relative overflow-hidden ${isActive
                                ? 'bg-primary-500/20 text-primary-neon shadow-neon-blue border border-primary-500/30'
                                : 'text-gray-500 dark:text-gray-400 hover:bg-white/10 hover:text-gray-100 hover:shadow-glass'
                                } ${isCollapsed ? 'justify-center' : ''}`}
                            title={isCollapsed ? item.label : ''}
                        >
                            {isActive && <div className="absolute inset-0 bg-primary-400/10 blur-xl" />}
                            <Icon size={22} className={`relative z-10 transition-transform duration-300 group-hover:scale-110 ${isActive ? 'text-primary-neon drop-shadow-md' : 'group-hover:text-white'} ${!isCollapsed ? 'mr-3' : ''}`} />
                            {!isCollapsed && <span className="font-medium relative z-10">{item.label}</span>}
                        </button>
                    );
                })}
            </nav>

            {/* Auth Actions */}
            <div className="px-3 pb-2 space-y-2">
                <button
                    onClick={() => {
                        onLoginClick();
                        closeSidebar();
                    }}
                    className={`w-full flex items-center px-3 py-2 rounded-xl text-gray-500 dark:text-gray-400 hover:bg-white/10 hover:text-white transition-all ${isCollapsed ? 'justify-center' : ''}`}
                    title={isCollapsed ? 'Dhan Login' : ''}
                >
                    <LogIn size={20} />
                    {!isCollapsed && <span className="ml-3 font-medium">Dhan Login</span>}
                </button>

                <button
                    onClick={() => {
                        onLogoutClick();
                        closeSidebar();
                    }}
                    className={`w-full flex items-center px-3 py-2 rounded-xl text-gray-500 dark:text-gray-400 hover:bg-white/10 hover:text-white transition-all ${isCollapsed ? 'justify-center' : ''}`}
                    title={isCollapsed ? 'Log out of Dhan' : ''}
                >
                    <LogOut size={20} />
                    {!isCollapsed && <span className="ml-3 font-medium">Log out</span>}
                </button>
            </div>

            {/* Collapse Toggle */}
            <div className="px-3 py-4">
                <button
                    onClick={toggleCollapse}
                    className={`w-full flex items-center px-3 py-2 rounded-xl text-gray-500 dark:text-gray-400 hover:bg-white/10 hover:text-white transition-all ${isCollapsed ? 'justify-center' : ''}`}
                >
                    {isCollapsed ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
                    {!isCollapsed && <span className="ml-3 font-medium">Collapse</span>}
                </button>
            </div>

        </div>
    );
}
