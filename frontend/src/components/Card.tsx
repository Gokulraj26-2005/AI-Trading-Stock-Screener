import React from 'react';

interface CardProps {
    children: React.ReactNode;
    className?: string;
    title?: string;
    action?: React.ReactNode;
    noPadding?: boolean;
}

export const Card: React.FC<CardProps> = ({ children, className = '', title, action, noPadding = false }) => {
    return (
        <div className={`glass-panel overflow-hidden ${className} relative flex flex-col transition-all duration-300 hover:shadow-glass-hover group`}>
            {(title || action) && (
                <div className="px-6 py-5 border-b border-white/10 flex justify-between items-center shrink-0 bg-white/5 backdrop-blur-sm">
                    {title && <h3 className="text-lg font-bold text-gray-800 dark:text-gray-100 group-hover:text-primary-neon transition-colors">{title}</h3>}
                    {action && <div>{action}</div>}
                </div>
            )}
            <div className={`${noPadding ? '' : 'p-6'} flex-1 w-full flex flex-col`}>
                {children}
            </div>
        </div>
    );
};
