import React, { useState } from 'react';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
interface LayoutProps {
    children: React.ReactNode;
    activeTab: string;
    onTabChange: (tab: string) => void;
    token: string;
    onLoginClick: () => void;
    onLogoutClick: () => void;
}

export function Layout({ children, activeTab, onTabChange, token, onLoginClick, onLogoutClick }: LayoutProps) {
    const [isCollapsed, setIsCollapsed] = useState(() => {
        if (typeof window !== 'undefined') {
            const saved = localStorage.getItem('sidebar_collapsed');
            if (saved !== null) {
                return saved === 'true';
            }
        }
        return true; // Default closed on site load
    });

    const toggleCollapse = () => {
        setIsCollapsed((prev) => {
            const next = !prev;
            if (typeof window !== 'undefined') {
                localStorage.setItem('sidebar_collapsed', String(next));
            }
            return next;
        });
    };

    const closeSidebar = () => {
        setIsCollapsed(true);
        if (typeof window !== 'undefined') {
            localStorage.setItem('sidebar_collapsed', 'true');
        }
    };

    const isAnalysis = activeTab === 'chart-analysis';

    return (
        <div className="h-screen w-screen font-sans text-gray-900 dark:text-gray-100 transition-colors relative bg-black overflow-hidden flex">

            <Sidebar
                activeTab={activeTab}
                onTabChange={onTabChange}
                isCollapsed={isCollapsed}
                toggleCollapse={toggleCollapse}
                closeSidebar={closeSidebar}
                onLoginClick={onLoginClick}
                onLogoutClick={onLogoutClick}
            />

            <div className={`transition-all duration-300 ${isCollapsed ? 'ml-24' : 'ml-72'} relative z-10 flex-1 h-screen flex flex-col min-w-0 overflow-hidden`}>
                <Header
                    token={token}
                    onTabChange={onTabChange}
                />
                <main className={`flex-1 min-h-0 overflow-hidden ${isAnalysis ? 'p-0 m-0' : 'p-8 mt-4'}`}>
                    {children}
                </main>
            </div>
        </div>
    );
}
