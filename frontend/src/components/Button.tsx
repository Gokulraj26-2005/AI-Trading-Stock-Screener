import React from 'react';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
    variant?: 'primary' | 'secondary' | 'outline' | 'ghost';
    size?: 'sm' | 'md' | 'lg';
}

export const Button: React.FC<ButtonProps> = ({
    children,
    variant = 'primary',
    size = 'md',
    className = '',
    ...props
}) => {
    const baseStyles = "inline-flex items-center justify-center font-bold transition-all duration-300 focus:outline-none focus:ring-2 focus:ring-offset-2 disabled:opacity-50 disabled:pointer-events-none rounded-xl active:scale-95";

    const variants = {
        primary: "bg-gradient-to-r from-primary-600 to-primary-500 hover:from-primary-500 hover:to-primary-400 text-white shadow-lg shadow-primary-500/30 hover:shadow-primary-500/50 border border-transparent",
        secondary: "bg-white/10 hover:bg-white/20 text-gray-800 dark:text-white border border-white/10 backdrop-blur-md shadow-glass hover:shadow-glass-hover",
        outline: "border-2 border-primary-500 text-primary-600 dark:text-primary-400 hover:bg-primary-500/10",
        ghost: "text-gray-600 dark:text-gray-300 hover:bg-white/10 hover:text-primary-neon",
    };

    const sizes = {
        sm: "h-8 px-3 text-xs",
        md: "h-10 px-5 py-2 text-sm",
        lg: "h-12 px-8 text-base",
    };

    return (
        <button
            className={`${baseStyles} ${variants[variant]} ${sizes[size]} ${className}`}
            {...props}
        >
            {children}
        </button>
    );
};
