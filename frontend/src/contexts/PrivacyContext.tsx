import React, { createContext, useContext, useState } from 'react';

interface PrivacyContextType {
    privacyMode: boolean;
    togglePrivacy: () => void;
    maskValue: (value: string | number) => string;
}

const PrivacyContext = createContext<PrivacyContextType | undefined>(undefined);

export function PrivacyProvider({ children }: { children: React.ReactNode }) {
    const [privacyMode, setPrivacyMode] = useState(false);

    const togglePrivacy = () => {
        setPrivacyMode(prev => !prev);
    };

    const maskValue = (value: string | number): string => {
        if (!privacyMode) {
            return String(value);
        }
        return '****';
    };

    return (
        <PrivacyContext.Provider value={{ privacyMode, togglePrivacy, maskValue }}>
            {children}
        </PrivacyContext.Provider>
    );
}

export function usePrivacy() {
    const context = useContext(PrivacyContext);
    if (!context) {
        throw new Error('usePrivacy must be used within PrivacyProvider');
    }
    return context;
}
