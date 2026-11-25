import React from 'react';

interface HeaderProps {
    token: string;
    onTabChange: (tab: string) => void;
}

export const Header = ({ token: _token, onTabChange: _onTabChange }: HeaderProps) => {
    return (
        <header className="fixed top-6 right-6 z-50 flex items-center gap-3 p-2 rounded-full glass-panel shadow-2xl border border-white/10 bg-black/20 backdrop-blur-xl transition-all duration-300 pointer-events-none opacity-0">
            {/* Header is intentionally hidden; kept for layout compatibility */}
        </header>
    );
};
