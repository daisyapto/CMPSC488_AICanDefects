/*****************************************************************************
* | File        :   DEV_Config.c
* | Author      :   Waveshare team
* | Function    :   Hardware underlying interface
* | Info        :
*                Used to shield the underlying layers of each master
*                and enhance portability
*----------------
* |	This version:   V1.0
* | Date        :   2018-09-03
* | Info        :   Basic version
*
******************************************************************************/
#include "DEV_Config.h"

#include <lgpio.h>

#define LFLAGS 0
#define NUM_MAXBUF  4
int GPIO_Handle;

/**
 * Module Initialize, use BCM2835 library.
 *
 * Example:
 * if(DEV_ModuleInit())
 *   exit(0);
 */
UBYTE DEV_ModuleInit(void)
{
    char buffer[NUM_MAXBUF];
    FILE *fp;
    fp = popen("cat /proc/cpuinfo | grep 'Raspberry Pi 5'", "r");
    if (fp == NULL) {
        DEBUG("It is not possible to determine the model of thFe Raspberry PI\n");
        return -1;
    }

    if(fgets(buffer, sizeof(buffer), fp) != NULL)
    {
        GPIO_Handle = lgGpiochipOpen(0);
        if (GPIO_Handle < 0)
        {
            DEBUG( "gpiochip4 Export Failed\n");
            return -1;
        }
    }
    else
    {
        GPIO_Handle = lgGpiochipOpen(0);
        if (GPIO_Handle < 0)
        {
            DEBUG( "gpiochip0 Export Failed\n");
            return -1;
        }
    }
    
    //motor 1 
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M1_ENABLE_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M1_DIR_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M1_STEP_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M1_M0_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M1_M1_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M1_M2_PIN, LG_LOW);

    //motor 2 
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M2_ENABLE_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M2_DIR_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M2_STEP_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M2_M0_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M2_M1_PIN, LG_LOW);
    lgGpioClaimOutput(GPIO_Handle, LFLAGS, M2_M2_PIN, LG_LOW);

    return 0;
}

/**
 * GPIO read and write
**/
void DEV_Digital_Write(UWORD Pin, UBYTE Value)
{
    lgGpioWrite(GPIO_Handle, Pin, Value);
}

/**
 * Module Exit, close BCM2835 library.
 *
 * Example:
 * DEV_ModuleExit();
 */
void DEV_ModuleExit(void)
{
    // lgGpiochipClose(GPIO_Handle);
}

/**
 * Millisecond delay.
 *
 * @param xms: time.
 *
 * Example:
 * DEV_Delay_ms(500);//delay 500ms
 */
void DEV_Delay_ms(uint32_t xms)
{
    lguSleep(xms/1000.0);
}

/**
 * Microsecond delay.
 *
 * @param xus: time.
 *
 * Example:
 * DEV_Delay_us(500);//delay 500us
 */
void DEV_Delay_us(uint32_t xus)
{
    lguSleep(xus/1000000.0);
}
