#if defined(STDINT_STUB) && !defined(_STDINT_H)
// `_STDINT_H` not defined, so no `stdint.h` included.
#define _STDINT_H
/*
 * From [here][1].
 *
 * 10.2 Basic data types in ARM C and C++
 * Describes the basic data types implemented in ARM C and C++:
 *
 * Size and alignment of basic data types
 * The following table gives the size and natural alignment of the basic data types.
 * Table 10-2 Size and alignment of data types
 *
 * Type               & Size in bits & Natural alignment in bytes & Range of values
 * char               & 8            & 1 (byte-aligned)           & 0 to 255 (unsigned) by default.
 *                    &              &                            & –128 to 127 (signed) when compiled with --signed_chars.
 * signed char        & 8            & 1 (byte-aligned)           & –128 to 127
 * unsigned char      & 8            & 1 (byte-aligned)           & 0 to 255
 * (signed) short     & 16           & 2 (halfword-aligned)       & –32,768 to 32,767
 * unsigned short     & 16           & 2 (halfword-aligned)       & 0 to 65,535
 * (signed) int       & 32           & 4 (word-aligned)           & –2,147,483,648 to 2,147,483,647
 * unsigned int       & 32           & 4 (word-aligned)           & 0 to 4,294,967,295
 * (signed) long      & 32           & 4 (word-aligned)           & –2,147,483,648 to 2,147,483,647
 * unsigned long      & 32           & 4 (word-aligned)           & 0 to 4,294,967,295
 * (signed) long long & 64           & 8 (doubleword-aligned)     & –9,223,372,036,854,775,808 to 9,223,372,036,854,775,807
 * unsigned long long & 64           & 8 (doubleword-aligned)     & 0 to 18,446,744,073,709,551,615
 * float              & 32           & 4 (word-aligned)           & 1.175494351e-38 to 3.40282347e+38 (normalized values)
 * double             & 64           & 8 (doubleword-aligned)     & 2.22507385850720138e-308 to 1.79769313486231571e+308 (normalized values)
 * long double        & 64           & 8 (doubleword-aligned)     & 2.22507385850720138e-308 to 1.79769313486231571e+308 (normalized values)
 * wchar_t            & 16           & 2 (halfword-aligned)       & 0 to 65,535 by default.
 *                    & 32           & 4 (word-aligned)           & 0 to 4,294,967,295 when compiled with --wchar32.
 * All pointers       & 32           & 4 (word-aligned)           & Not applicable.
 * bool (C++ only)    & 8            & 1 (byte-aligned)           & false or true
 * _Bool (C only)     & 8            & 1 (byte-aligned)           & false or true
 *
 *
 * [1]: http://www.keil.com/support/man/docs/armcc/armcc_chr1359125009502.htm
 */
/* **TODO** Create const variables exposing size of types.
 *
 * ## 8-bit ##
 *
 * char               & 8
 * signed char        & 8
 * unsigned char      & 8
 */
typedef signed char int8_t;
typedef unsigned char uint8_t;
/*
 * ## 16-bit ##
 *
 * (signed) short     & 16
 * unsigned short     & 16
 */
typedef signed short int16_t;
typedef unsigned short uint16_t;
/*
 * ## 32-bit ##
 *
 * (signed) int       & 32
 * unsigned int       & 32
 * (signed) long      & 32
 * unsigned long      & 32
 */
typedef signed int int32_t;
typedef unsigned int uint32_t;
/*
 * ## 64-bit ##
 *
 * (signed) long long & 64
 * unsigned long long & 64
 */
typedef signed long long int64_t;
typedef unsigned long long uint64_t;
/*
 * ## Floating point ##
 *
 * float              & 32
 * double             & 64
 * long double        & 64
 */
/*
 * wchar_t            & 16
 *                    & 32
 * All pointers       & 32
 * bool (C++ only)    & 8
 * _Bool (C only)     & 8
 */

#endif  // #if defined(STDINT_STUB) && !defined(_STDINT_H)
