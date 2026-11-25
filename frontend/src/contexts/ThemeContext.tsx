import React, { createContext, useContext, useEffect } from 'react';

interface ThemeSettings {
    primaryColor: string;
    secondaryColor: string;
    animationSpeed: number;
    blobOpacity: number;
    isDynamicLightingEnabled: boolean;
    isLowPowerMode: boolean;
    textColor: string;
    neonColor: string;
}

interface ThemeContextType extends ThemeSettings {
    theme: 'dark';
    toggleTheme: () => void;
    updateSettings: (settings: Partial<ThemeSettings>) => void;
    resetTheme: () => void;
}

const defaultSettings: ThemeSettings = {
    primaryColor: '#8b5cf6',
    secondaryColor: '#ec4899',
    animationSpeed: 5,
    blobOpacity: 0.3,
    isDynamicLightingEnabled: true,
    isLowPowerMode: false,
    textColor: '',
    neonColor: '',
};

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
    // Force Dark Mode
    const theme = 'dark';

    useEffect(() => {
        document.documentElement.classList.add('dark');
    }, []);

    // Placeholder functions since customization is removed
    const toggleTheme = () => {};
    const updateSettings = () => {};
    const resetTheme = () => {};

    return (
        <ThemeContext.Provider value={{ 
            theme, 
            toggleTheme, 
            ...defaultSettings, 
            updateSettings, 
            resetTheme 
        }}>
            {children}
        </ThemeContext.Provider>
    );
}

export function useTheme() {
    const context = useContext(ThemeContext);
    if (!context) {
        throw new Error('useTheme must be used within ThemeProvider');
    }
    return context;
}
